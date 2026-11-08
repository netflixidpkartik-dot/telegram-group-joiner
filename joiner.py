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
                await self.notifier_cb(message)
            except Exception as e:
                logger.error(f"Error calling notifier_cb: {e}")

    async def mute_peer(self, peer_entity):
        try:
            await self.client(account.UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer_entity),
                settings=InputPeerNotifySettings(
                    mute_until=2147483647,
                    silent=True,
                    show_previews=False
                )
            ))
            return True
        except Exception as e:
            logger.debug(f"Failed to mute peer {peer_entity}: {e}")
            return False

    async def sync_existing_dialogs(self):
        """Scans existing groups in account and adds them to storage & grps folder"""
        await self.notify("🔄 Syncing existing dialogs in account...")
        try:
            dialogs = await self.client.get_dialogs(limit=200)
            count = 0
            for d in dialogs:
                try:
                    if d.is_group or d.is_channel:
                        ent = d.entity
                        if isinstance(ent, (Channel, Chat)):
                            if getattr(ent, 'broadcast', False) and not getattr(ent, 'megagroup', False):
                                continue
                            acc_hash = getattr(ent, 'access_hash', 0)
                            storage.record_joined(d.id, d.name, getattr(ent, 'username', ''), acc_hash)
                            if d.input_entity:
                                await self.mute_peer(d.input_entity)
                            count += 1
                except Exception as de:
                    logger.debug(f"Error checking dialog {d.name}: {de}")
            logger.info(f"Synced {count} existing groups from account.")
            await self.refresh_folder_and_link()
        except Exception as e:
            logger.error(f"Error syncing existing dialogs: {e}")

    async def refresh_folder_and_link(self):
        """Builds DialogFilterChatlist and exports/updates the shareable link"""
        try:
            input_peers = []
            joined = list(storage.data["joined_groups"].values())
            # Telegram non-premium chatlist limit is 100 peers
            for g in joined[:100]:
                cid = g["id"]
                acc_h = g.get("access_hash")
                try:
                    if acc_h:
                        # Extract channel ID if -100 format
                        raw_id = int(str(cid).replace("-100", "").replace("-", ""))
                        input_peers.append(InputPeerChannel(raw_id, int(acc_h)))
                    else:
                        ent = await self.client.get_input_entity(cid)
                        input_peers.append(ent)
                except Exception as pe:
                    logger.debug(f"Could not resolve input peer for {cid}: {pe}")

            if not input_peers:
                logger.info("No input peers available to create chatlist yet.")
                return None

            # 1. Update DialogFilterChatlist
            f = DialogFilterChatlist(
                id=config.FOLDER_ID,
                title=TextWithEntities(text=config.FOLDER_NAME, entities=[]),
                pinned_peers=[],
                include_peers=input_peers,
                has_my_invites=True
            )
            await self.client(messages.UpdateDialogFilterRequest(id=config.FOLDER_ID, filter=f))
            logger.info(f"Updated folder '{config.FOLDER_NAME}' with {len(input_peers)} peers.")

            # 2. Export / Update Chatlist Invite Link
            chatlist_filter = InputChatlistDialogFilter(filter_id=config.FOLDER_ID)
            slug = storage.data.get("chatlist_slug", "")
            url = ""

            if slug:
                try:
                    res = await self.client(chatlists.EditExportedInviteRequest(
                        chatlist=chatlist_filter,
                        slug=slug,
                        title=config.FOLDER_NAME,
                        peers=input_peers
                    ))
                    url = res.invite.url
                    logger.info(f"Updated existing chatlist link: {url}")
                except Exception as e:
                    logger.warning(f"EditExportedInviteRequest failed: {e}. Re-exporting...")
                    slug = ""

            if not slug:
                res = await self.client(chatlists.ExportChatlistInviteRequest(
                    chatlist=chatlist_filter,
                    title=config.FOLDER_NAME,
                    peers=input_peers
                ))
                url = res.invite.url
                new_slug = url.split('/')[-1]
                storage.set_folder_link(url, slug=new_slug)
                logger.info(f"Created new combined chatlist link: {url}")
                await self.notify(f"🔗 <b>New Combined Folder Link Generated:</b>\n{url}")

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
                invite = await self.client(chatlists.CheckChatlistInviteRequest(slug=val))
                if isinstance(invite, chatlists.tl.types.chatlists.ChatlistInvite):
                    missing = getattr(invite, 'missing_peers', []) or getattr(invite, 'peers', [])
                    if missing:
                        await self.client(chatlists.JoinChatlistInviteRequest(slug=val, peers=missing))
                    
                    # Mute and record all chats in folder
                    chats = getattr(invite, 'chats', [])
                    for ch in chats:
                        if hasattr(ch, 'id'):
                            acc_h = getattr(ch, 'access_hash', 0)
                            storage.record_joined(ch.id, getattr(ch, 'title', ''), getattr(ch, 'username', ''), acc_h)
                            try:
                                ent = await self.client.get_input_entity(ch)
                                await self.mute_peer(ent)
                            except Exception:
                                pass
                    await self.notify(f"✅ Joined Chatlist Folder <b>{getattr(invite, 'title', val)}</b> ({len(chats)} groups) & muted!")
                    return True
                elif isinstance(invite, chatlists.tl.types.chatlists.ChatlistInviteAlready):
                    await self.notify(f"ℹ️ Already joined Chatlist Folder: {val}")
                    return True
            except Exception as e:
                logger.warning(f"Failed to join chatlist {val}: {e}")
                storage.record_failed(raw_item, str(e))
                return False

        # 2. Private Invite Hash
        elif link_type == "invite_hash":
            try:
                res = await self.client(messages.ImportChatInviteRequest(hash=val))
                chats = getattr(res, 'chats', [])
                for ch in chats:
                    acc_h = getattr(ch, 'access_hash', 0)
                    storage.record_joined(ch.id, getattr(ch, 'title', ''), getattr(ch, 'username', ''), acc_h)
                    ent = await self.client.get_input_entity(ch)
                    await self.mute_peer(ent)
                    await self.notify(f"✅ Joined Private Group <b>{getattr(ch, 'title', '')}</b> & muted!")
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
                ent = await self.client.get_input_entity(val)
                await self.client(channels.JoinChannelRequest(channel=ent))
                full_ent = await self.client.get_entity(ent)
                acc_h = getattr(full_ent, 'access_hash', 0)
                storage.record_joined(full_ent.id, getattr(full_ent, 'title', val), val, acc_h)
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
        await self.sync_existing_dialogs()
        
        # Ensure INITIAL_LINKS are queued if queue is currently empty
        if not storage.data["queue"] and not storage.data["joined_groups"]:
            storage.add_to_queue(config.INITIAL_LINKS)
            logger.info(f"Loaded {len(config.INITIAL_LINKS)} initial links into queue.")

        cycle_count = 0
        while self.is_running:
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
                    await self.refresh_folder_and_link()

                delay = random.uniform(config.DELAY_BETWEEN_JOINS_MIN, config.DELAY_BETWEEN_JOINS_MAX)
                storage.set_status(f"Safe sleep for {delay:.1f}s...")
                await asyncio.sleep(delay)

            except FloodWaitError as e:
                wait_sec = e.seconds + 5
                storage.set_status(f"⚠️ Telegram FloodWait: Sleeping for {wait_sec}s...")
                await self.notify(f"⏳ <b>FloodWait Notice:</b> Telegram asked to wait {wait_sec}s. Sleeping before resuming...")
                await asyncio.sleep(wait_sec)
                # Re-queue the item
                storage.data["queue"].insert(0, item)
            except Exception as e:
                logger.error(f"Unexpected error processing {item}: {e}")
                await asyncio.sleep(5)
