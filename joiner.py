import asyncio
import logging
import random
import re
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions import messages, channels, chatlists, account
from telethon.tl.types import (
    DialogFilterChatlist, TextWithEntities, InputChatlistDialogFilter,
    InputNotifyPeer, InputPeerNotifySettings, InputPeerChannel, InputPeerChat,
    PeerChannel, PeerChat, Channel, Chat
)
from telethon.tl.types.chatlists import ChatlistInvite, ChatlistInviteAlready
from telethon.errors import (
    FloodWaitError, UserAlreadyParticipantError, InviteHashExpiredError,
    InviteHashInvalidError, ChannelPrivateError, InviteRequestSentError
)
import config
from storage import storage

logger = logging.getLogger("JoinerEngine")

def parse_link_type(link_or_text):
    text = str(link_or_text).strip()
    # 1. Chatlist Addlist slug
    m_addlist = re.search(r'(?:t\.me/addlist/|tg://addlist\?slug=)([a-zA-Z0-9_-]+)', text)
    if m_addlist:
        return "addlist", m_addlist.group(1)
        
    # 2. Private invite hash
    m_hash = re.search(r'(?:t\.me/(?:\+|joinchat/))([a-zA-Z0-9_-]+)', text)
    if m_hash:
        return "invite_hash", m_hash.group(1)
        
    # 3. Public username
    m_user = re.search(r'(?:(?:https?://)?t\.me/|@)([a-zA-Z0-9_]{4,32})', text)
    if m_user:
        u = m_user.group(1)
        if u.lower() not in ('addlist', 'joinchat', 'c', 'share', 'contact'):
            return "username", u
            
    # Fallback: check if the string itself is a slug
    if len(text) > 10 and not text.startswith("http") and not text.startswith("@"):
        return "addlist", text
        
    return "unknown", text

class JoinerWorker:
    def __init__(self, user_client: TelegramClient, notifier_cb=None):
        self.client = user_client
        self.notifier_cb = notifier_cb
        self.is_running = True

    async def notify(self, message):
        logger.info(message)
        if self.notifier_cb:
            try:
                asyncio.create_task(self.notifier_cb(message))
            except Exception as e:
                logger.error(f"Error dispatching notifier_cb: {e}")

    async def mute_peer(self, peer_entity):
        try:
            await asyncio.wait_for(
                self.client(account.UpdateNotifySettingsRequest(
                    peer=InputNotifyPeer(peer_entity),
                    settings=InputPeerNotifySettings(
                        mute_until=2147483647,
                        silent=True,
                        show_previews=False
                    )
                )),
                timeout=8.0
            )
            return True
        except Exception as e:
            logger.debug(f"Failed to mute peer: {e}")
            return False

    async def sync_existing_dialogs(self):
        """Scans existing groups in account and adds them to storage & grps folder"""
        await self.notify("🔄 Syncing existing dialogs in account...")
        try:
            dialogs = await asyncio.wait_for(self.client.get_dialogs(limit=50), timeout=15.0)
            count = 0
            for d in dialogs:
                try:
                    if d.is_group or d.is_channel:
                        ent = d.entity
                        if isinstance(ent, (Channel, Chat)):
                            if getattr(ent, 'broadcast', False) and not getattr(ent, 'megagroup', False):
                                continue
                            acc_hash = getattr(ent, 'access_hash', 0)
                            safe_name = str(d.name).encode('ascii', errors='ignore').decode('ascii') or f"Group_{d.id}"
                            storage.record_joined(d.id, safe_name, getattr(ent, 'username', ''), acc_hash)
                            if d.input_entity:
                                await self.mute_peer(d.input_entity)
                            count += 1
                except Exception as de:
                    logger.debug(f"Error checking dialog: {de}")
            logger.info(f"Synced {count} existing groups from account.")
            await self.refresh_folder_and_link()
        except Exception as e:
            logger.error(f"Error syncing existing dialogs: {e}")

    async def refresh_folder_and_link(self):
        """Builds DialogFilterChatlist from LIVE account dialogs and exports/updates the shareable link"""
        try:
            # --- Get LIVE groups from account dialogs (source of truth) ---
            input_peers = []
            live_peer_ids = set()
            try:
                dialogs = await asyncio.wait_for(self.client.get_dialogs(limit=200), timeout=15.0)
                for d in dialogs:
                    if not (d.is_group or d.is_channel):
                        continue
                    ent = d.entity
                    # Skip pure broadcast channels (not supergroups)
                    if getattr(ent, 'broadcast', False) and not getattr(ent, 'megagroup', False):
                        continue
                    acc_h = getattr(ent, 'access_hash', None)
                    if acc_h and d.input_entity:
                        raw_id = int(str(d.id).replace("-100", "").replace("-", ""))
                        input_peers.append(InputPeerChannel(raw_id, acc_h))
                        live_peer_ids.add(d.id)
                        # Also update storage with correct access hash
                        storage.record_joined(d.id, str(d.name).encode('ascii', errors='replace').decode(),
                                              getattr(ent, 'username', ''), acc_h)
                logger.info(f"refresh_folder_and_link: got {len(input_peers)} live peers from account")
            except Exception as de:
                logger.warning(f"Could not get live dialogs, falling back to storage: {de}")
                # Fallback: use storage
                joined = list(storage.data["joined_groups"].values())
                for g in joined[:100]:
                    cid = g["id"]
                    acc_h = g.get("access_hash")
                    try:
                        if acc_h:
                            raw_id = int(str(cid).replace("-100", "").replace("-", ""))
                            input_peers.append(InputPeerChannel(raw_id, int(acc_h)))
                        else:
                            ent = await asyncio.wait_for(self.client.get_input_entity(cid), timeout=8.0)
                            input_peers.append(ent)
                    except Exception as pe:
                        logger.debug(f"Could not resolve input peer for {cid}: {pe}")

            # Telegram non-premium chatlist hard limit: 100 peers
            input_peers = input_peers[:100]

            if not input_peers:
                logger.info("No live groups in account yet. Folder update skipped.")
                return None

            # 1. Update the DialogFilterChatlist (folder)
            f = DialogFilterChatlist(
                id=config.FOLDER_ID,
                title=TextWithEntities(text=config.FOLDER_NAME, entities=[]),
                pinned_peers=[],
                include_peers=input_peers,
                has_my_invites=True
            )
            await asyncio.wait_for(
                self.client(messages.UpdateDialogFilterRequest(id=config.FOLDER_ID, filter=f)),
                timeout=12.0
            )
            logger.info(f"Updated folder '{config.FOLDER_NAME}' with {len(input_peers)} LIVE peers.")

            # 2. Export / Update Chatlist Invite Link
            chatlist_filter = InputChatlistDialogFilter(filter_id=config.FOLDER_ID)
            existing_invites = []
            try:
                inv_res = await asyncio.wait_for(
                    self.client(chatlists.GetExportedInvitesRequest(chatlist=chatlist_filter)),
                    timeout=10.0
                )
                existing_invites = getattr(inv_res, 'invites', [])
            except Exception as ie:
                logger.debug(f"Could not fetch existing invites: {ie}")

            url = ""
            if existing_invites:
                main_inv = existing_invites[0]
                main_slug = main_inv.url.split('/')[-1]
                # Remove extra invites to avoid INVITES_TOO_MUCH
                for extra in existing_invites[1:]:
                    try:
                        await self.client(chatlists.DeleteExportedInviteRequest(
                            chatlist=chatlist_filter, slug=extra.url.split('/')[-1]))
                    except Exception:
                        pass
                try:
                    res = await asyncio.wait_for(
                        self.client(chatlists.EditExportedInviteRequest(
                            chatlist=chatlist_filter,
                            slug=main_slug,
                            title=config.FOLDER_NAME,
                            peers=input_peers
                        )),
                        timeout=12.0
                    )
                    url = getattr(res, 'url', main_inv.url)
                    storage.set_folder_link(url, slug=main_slug)
                    logger.info(f"Updated chatlist invite link: {url} ({len(input_peers)} peers)")
                except Exception as ee:
                    logger.warning(f"EditExportedInvite failed: {ee}. Using existing URL.")
                    url = main_inv.url
                    storage.set_folder_link(url, slug=main_slug)
            else:
                res = await asyncio.wait_for(
                    self.client(chatlists.ExportChatlistInviteRequest(
                        chatlist=chatlist_filter,
                        title=config.FOLDER_NAME,
                        peers=input_peers
                    )),
                    timeout=12.0
                )
                url = getattr(res, 'url', getattr(getattr(res, 'invite', None), 'url', ''))
                new_slug = url.split('/')[-1]
                storage.set_folder_link(url, slug=new_slug)
                logger.info(f"Created new chatlist invite link: {url}")
                await self.notify(f"🔗 <b>Combined Folder Link:</b>\n{url}")

            return url
        except Exception as e:
            logger.error(f"Error in refresh_folder_and_link: {e}")
            return None

    async def process_item(self, raw_item):
        link_type, val = parse_link_type(raw_item)
        logger.info(f"Processing ({link_type}): {val}")
        storage.set_status(f"Processing ({link_type}): {val}")

        # 1. Chatlist Folder
        if link_type == "addlist":
            try:
                invite = await asyncio.wait_for(self.client(chatlists.CheckChatlistInviteRequest(slug=val)), timeout=15.0)
                if isinstance(invite, ChatlistInvite):
                    input_peers = []
                    chats = getattr(invite, 'chats', [])
                    for ch in chats:
                        if hasattr(ch, 'id') and hasattr(ch, 'access_hash'):
                            input_peers.append(InputPeerChannel(ch.id, ch.access_hash))
                    
                    if input_peers:
                        try:
                            await asyncio.wait_for(self.client(chatlists.JoinChatlistInviteRequest(slug=val, peers=input_peers)), timeout=15.0)
                        except Exception as je:
                            logger.info(f"JoinChatlistInviteRequest notice: {je}")

                    # Record chats & mute them
                    title_str = getattr(invite.title, 'text', str(invite.title)) if hasattr(invite, 'title') else val
                    for ch in chats:
                        if hasattr(ch, 'id'):
                            acc_h = getattr(ch, 'access_hash', 0)
                            safe_ch_title = str(getattr(ch, 'title', '')).encode('ascii', errors='ignore').decode('ascii')
                            storage.record_joined(ch.id, safe_ch_title, getattr(ch, 'username', ''), acc_h)
                            if hasattr(ch, 'access_hash'):
                                await self.mute_peer(InputPeerChannel(ch.id, ch.access_hash))
                                
                    await self.notify(f"✅ Joined Chatlist Folder <b>{title_str}</b> ({len(chats)} groups) & muted!")
                    return True
                elif isinstance(invite, ChatlistInviteAlready):
                    await self.notify(f"ℹ️ Already joined Chatlist Folder: {val}")
                    return True
            except Exception as e:
                logger.warning(f"Failed to join chatlist {val}: {e}")
                storage.record_failed(raw_item, str(e))
                return False

        # 2. Private Invite Hash
        elif link_type == "invite_hash":
            try:
                res = await asyncio.wait_for(self.client(messages.ImportChatInviteRequest(hash=val)), timeout=15.0)
                chats = getattr(res, 'chats', [])
                for ch in chats:
                    acc_h = getattr(ch, 'access_hash', 0)
                    safe_ch_title = str(getattr(ch, 'title', '')).encode('ascii', errors='ignore').decode('ascii')
                    storage.record_joined(ch.id, safe_ch_title, getattr(ch, 'username', ''), acc_h)
                    ent = await asyncio.wait_for(self.client.get_input_entity(ch), timeout=8.0)
                    await self.mute_peer(ent)
                    await self.notify(f"✅ Joined Private Group <b>{safe_ch_title}</b> & muted!")
                return True
            except UserAlreadyParticipantError:
                logger.info(f"Already in group with hash {val}")
                return True
            except (InviteHashExpiredError, InviteHashInvalidError) as e:
                logger.warning(f"Invite hash expired/invalid: {val}")
                storage.record_failed(raw_item, f"Expired/Invalid: {e}")
                return False
            except Exception as e:
                logger.error(f"Error joining hash {val}: {e}")
                storage.record_failed(raw_item, str(e))
                return False

        # 3. Public Username
        elif link_type == "username":
            try:
                ent = await asyncio.wait_for(self.client.get_input_entity(val), timeout=10.0)
                await asyncio.wait_for(self.client(channels.JoinChannelRequest(channel=ent)), timeout=15.0)
                full_ent = await asyncio.wait_for(self.client.get_entity(ent), timeout=10.0)
                acc_h = getattr(full_ent, 'access_hash', 0)
                safe_title = str(getattr(full_ent, 'title', val)).encode('ascii', errors='ignore').decode('ascii')
                storage.record_joined(full_ent.id, safe_title, val, acc_h)
                await self.mute_peer(ent)
                await self.notify(f"✅ Joined Public Group <b>@{val}</b> & muted!")
                return True
            except UserAlreadyParticipantError:
                logger.info(f"Already in group @{val}")
                return True
            except InviteRequestSentError:
                await self.notify(f"📩 Join request sent for @{val} (Admin approval pending)")
                return True
            except ChannelPrivateError as e:
                logger.warning(f"Channel @{val} is private or banned: {e}")
                storage.record_failed(raw_item, "Private or Banned")
                return False
            except Exception as e:
                logger.error(f"Error joining @{val}: {e}")
                storage.record_failed(raw_item, str(e))
                return False

        else:
            storage.record_failed(raw_item, "Unsupported link format")
            return False

    async def run_loop(self):
        """Continuously pulls from queue, joins group, mutes, and periodically updates folder"""
        logger.info("JoinerEngine loop initiated.")
        
        # Ensure INITIAL_LINKS are queued if queue is empty
        if not storage.data["queue"]:
            added = storage.add_to_queue(config.INITIAL_LINKS)
            if added:
                logger.info(f"Loaded {added} initial links into queue.")

        storage.set_status("Joiner engine active - Processing queued links...")
        logger.info(f"Joiner engine active. Queue size: {len(storage.data['queue'])}")

        # Launch dialog sync in background so queue processing starts immediately without blocking
        asyncio.create_task(self.sync_existing_dialogs())

        cycle_count = 0
        while self.is_running:
            try:
                item = storage.pop_queue()
                if not item:
                    storage.set_status("Idle - Queue completed! Waiting for new links.")
                    await asyncio.sleep(5)
                    continue

                try:
                    await self.process_item(item)
                    cycle_count += 1

                    # Refresh folder and combined chatlist link every 5 items or when queue is empty
                    if cycle_count % 5 == 0 or len(storage.data["queue"]) == 0:
                        try:
                            await self.refresh_folder_and_link()
                        except Exception as re:
                            logger.error(f"Error during periodic refresh_folder_and_link: {re}")

                    delay = random.uniform(config.DELAY_BETWEEN_JOINS_MIN, config.DELAY_BETWEEN_JOINS_MAX)
                    storage.set_status(f"Safe sleep for {delay:.1f}s...")
                    await asyncio.sleep(delay)

                except FloodWaitError as e:
                    wait_sec = e.seconds + 5
                    storage.set_status(f"⚠️ Telegram FloodWait: Sleeping for {wait_sec}s...")
                    await self.notify(f"⏳ <b>FloodWait Notice:</b> Telegram asked to wait {wait_sec}s. Sleeping before resuming...")
                    # Re-queue the item
                    storage.data["queue"].insert(0, item)
                    await asyncio.sleep(wait_sec)
                except Exception as e:
                    logger.error(f"Unexpected error processing {item}: {e}")
                    await asyncio.sleep(5)
            except Exception as outer_err:
                logger.error(f"Unexpected loop exception: {outer_err}")
                await asyncio.sleep(5)
