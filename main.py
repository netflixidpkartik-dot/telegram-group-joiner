import asyncio
import logging
import os
import re
import sys
import json
import threading
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.custom import Button

import config
from storage import storage
from joiner import JoinerWorker

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout
)
logger = logging.getLogger("Main")

bot_client = TelegramClient("bot_session", config.API_ID, config.API_HASH)
user_client = TelegramClient(StringSession(config.SESSION_STRING), config.API_ID, config.API_HASH)

async def send_admin_notification(text):
    for admin_id in config.ADMIN_IDS:
        try:
            await asyncio.wait_for(bot_client.send_message(admin_id, text, parse_mode="html"), timeout=3.0)
        except Exception:
            pass

joiner_worker = JoinerWorker(user_client, notifier_cb=send_admin_notification)

def extract_all_links(text):
    raw_text = str(text)
    pattern = re.compile(r'(?:https?://t\.me/(?:addlist/[a-zA-Z0-9_\-]+|\+[a-zA-Z0-9_\-]+|[a-zA-Z0-9_]+/[0-9]+|[a-zA-Z0-9_]+)|@[a-zA-Z0-9_]{4,32})')
    found = pattern.findall(raw_text)
    clean = []
    for f in found:
        # Strip trailing numeric suffix /1
        f_clean = re.sub(r'/[0-9]+$', '', f)
        if any(ign in f_clean.lower() for ign in ['bot', 'addlist/c', 'joinchat/c', 'share', 'contact']):
            if 'addlist/' not in f_clean:
                continue
        clean.append(f_clean)
    return list(dict.fromkeys(clean))

def build_status_message():
    stats = storage.get_stats()
    folder_link = stats["combined_folder_link"] or "Generating in background..."
    
    msg = (
        "<b>📁 TELEGRAM GROUP JOINER & FOLDER BOT</b>\n\n"
        "<b>📱 Target Account:</b> <code>+8801300649370</code>\n"
        f"<b>📂 Telegram Folder:</b> <code>{config.FOLDER_NAME}</code>\n"
        f"<b>⚡ Engine Status:</b> <i>{stats['status_text']}</i>\n\n"
        f"<b>📊 Statistics:</b>\n"
        f"  • <b>Clean Groups in 'grps':</b> <code>{stats['joined_count']}</code>\n"
        f"  • <b>Remaining in Queue:</b> <code>{stats['queue_len']}</code>\n"
        f"  • <b>Failed / Restricted:</b> <code>{stats['failed_count']}</code>\n\n"
        "<b>🔗 Master Combined Folder Link:</b>\n"
        f"<code>{folder_link}</code>\n\n"
        "<i>💡 Paste links to add to queue, or use /retry to retry failed groups!</i>"
    )
    return msg

@bot_client.on(events.NewMessage(pattern=r'^/start'))
async def start_handler(event):
    buttons = [
        [Button.inline("🔗 Folder Link", data="get_link"), Button.inline("📊 Refresh Status", data="refresh_status")],
        [Button.inline("🔄 Retry Failed", data="retry_failed"), Button.inline("🧹 Clean Restricted", data="clean_restricted")],
        [Button.inline("🔄 Force Re-Export Link", data="force_export")]
    ]
    await event.reply(build_status_message(), parse_mode="html", buttons=buttons)

@bot_client.on(events.NewMessage(pattern=r'^/link'))
async def link_handler(event):
    stats = storage.get_stats()
    link = stats["combined_folder_link"]
    if link:
        buttons = [[Button.url("📁 Open Combined Folder", url=link)]]
        await event.reply(
            f"<b>🔗 Combined Folder Link:</b>\n\n<code>{link}</code>\n\n"
            f"<i>Contains {stats['joined_count']} clean groups in folder '{config.FOLDER_NAME}'!</i>",
            parse_mode="html",
            buttons=buttons
        )
    else:
        await event.reply("⏳ Combined folder link is currently being generated. Please check back in 1-2 minutes or use /status.", parse_mode="html")

@bot_client.on(events.NewMessage(pattern=r'^/status'))
async def status_handler(event):
    buttons = [
        [Button.inline("📊 Refresh Status", data="refresh_status"), Button.inline("🔗 Get Link", data="get_link")],
        [Button.inline("🔄 Retry Failed", data="retry_failed"), Button.inline("🧹 Clean Restricted", data="clean_restricted")]
    ]
    await event.reply(build_status_message(), parse_mode="html", buttons=buttons)

@bot_client.on(events.NewMessage(pattern=r'^/retry'))
async def retry_handler(event):
    added = storage.requeue_failed()
    # Also add any initial links not yet in queue
    added += storage.add_to_queue(config.INITIAL_LINKS)
    await event.reply(
        f"🔄 <b>Retry Initiated!</b>\n"
        f"Re-queued <code>{added}</code> links back into active processing queue!\n"
        f"Current Queue Length: <code>{len(storage.data['queue'])}</code>\n\n"
        f"<i>The joiner engine will now resume processing and join the groups.</i>",
        parse_mode="html"
    )

@bot_client.on(events.NewMessage(pattern=r'^/clean'))
async def clean_handler(event):
    await event.reply("🧹 <b>Starting group audit & cleanup...</b>\nChecking for captcha and messaging restrictions...", parse_mode="html")
    clean_count, restr_count = await joiner_worker.clean_account_and_folder()
    await event.reply(
        f"✅ <b>Audit Complete!</b>\n"
        f"• Removed & left <code>{restr_count}</code> restricted groups\n"
        f"• Kept <code>{clean_count}</code> clean groups in folder '{config.FOLDER_NAME}'\n"
        f"• Folder link updated: <code>{storage.data.get('combined_folder_link', '')}</code>",
        parse_mode="html"
    )

@bot_client.on(events.NewMessage(pattern=r'^/export'))
async def export_handler(event):
    await event.reply("🔄 Re-exporting combined chatlist folder link in background...", parse_mode="html")
    link = await joiner_worker.refresh_folder_and_link()
    if link:
        buttons = [[Button.url("📁 Open Folder", url=link)]]
        await event.reply(f"✅ <b>Combined Folder Link Refreshed:</b>\n\n<code>{link}</code>", parse_mode="html", buttons=buttons)
    else:
        await event.reply("⚠️ Could not export link yet. Ensure at least one group is joined.", parse_mode="html")

@bot_client.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data.decode("utf-8")
    if data == "get_link":
        stats = storage.get_stats()
        link = stats["combined_folder_link"]
        if link:
            await event.respond(f"<b>🔗 Combined Folder Link:</b>\n<code>{link}</code>", parse_mode="html")
        else:
            await event.answer("Link is still generating...", alert=True)
    elif data == "refresh_status":
        buttons = [
            [Button.inline("🔗 Folder Link", data="get_link"), Button.inline("📊 Refresh Status", data="refresh_status")],
            [Button.inline("🔄 Retry Failed", data="retry_failed"), Button.inline("🧹 Clean Restricted", data="clean_restricted")],
            [Button.inline("🔄 Force Re-Export Link", data="force_export")]
        ]
        try:
            await event.edit(build_status_message(), parse_mode="html", buttons=buttons)
        except Exception:
            pass
        await event.answer("Status refreshed!")
    elif data == "retry_failed":
        await event.answer("Re-queuing failed groups...")
        added = storage.requeue_failed()
        added += storage.add_to_queue(config.INITIAL_LINKS)
        await event.respond(f"🔄 Re-queued <b>{added}</b> links to retry. Current queue: <b>{len(storage.data['queue'])}</b>", parse_mode="html")
    elif data == "clean_restricted":
        await event.answer("Cleaning restricted groups...")
        clean_count, restr_count = await joiner_worker.clean_account_and_folder()
        await event.respond(f"🧹 Cleaned! Kept <b>{clean_count}</b> clean groups, removed <b>{restr_count}</b> restricted.", parse_mode="html")
    elif data == "force_export":
        await event.answer("Re-exporting folder link...")
        link = await joiner_worker.refresh_folder_and_link()
        if link:
            await event.respond(f"✅ <b>Folder Link Updated:</b>\n<code>{link}</code>", parse_mode="html")
        else:
            await event.respond("⚠️ No groups ready to export yet.", parse_mode="html")

@bot_client.on(events.NewMessage)
async def message_handler(event):
    text = event.raw_text.strip()
    if text.startswith("/"):
        return  # Handled by command handlers
        
    extracted = extract_all_links(text)
    if extracted:
        added = storage.add_to_queue(extracted)
        await event.reply(
            f"📥 <b>Received {len(extracted)} link(s)!</b>\n"
            f"➕ <b>Added to Auto-Join Queue:</b> <code>{added}</code> new link(s)\n"
            f"⏳ <b>Total Queue Remaining:</b> <code>{len(storage.data['queue'])}</code>\n\n"
            f"<i>The bot will safely auto-join each group, permanently mute notifications, and update the master folder link!</i>",
            parse_mode="html"
        )

async def main():
    logger.info("Starting Telegram Group Joiner Bot & Engine...")
    
    # 1. Start Bot client
    await bot_client.start(bot_token=config.BOT_TOKEN)
    bot_me = await bot_client.get_me()
    logger.info(f"Bot connected: @{bot_me.username} ({bot_me.first_name})")
    
    # 2. Start User client
    await user_client.start()
    user_me = await user_client.get_me()
    logger.info(f"User account connected: {user_me.first_name} (+{user_me.phone}) ID: {user_me.id}")
    
    # Pre-seed initial links if storage is empty, or requeue if queue is empty
    if not storage.data["queue"]:
        requeued = storage.requeue_failed()
        added = storage.add_to_queue(config.INITIAL_LINKS)
        logger.info(f"Loaded {added + requeued} links into storage queue on startup.")
        
    # Send start notification to admins
    await send_admin_notification(
        f"🚀 <b>Telegram Group Joiner Bot Online 24/7!</b>\n\n"
        f"📱 <b>Host Account:</b> <code>+{user_me.phone}</code> ({user_me.first_name})\n"
        f"🤖 <b>Bot Handle:</b> @{bot_me.username}\n"
        f"📂 <b>Target Folder:</b> <code>{config.FOLDER_NAME}</code>\n"
        f"⏳ <b>Queued Groups/Folders:</b> <code>{len(storage.data['queue'])}</code>\n\n"
        f"<i>Broadcasting & folder combiner background loop started.</i>"
    )
    
    # Run both bot and background joiner engine concurrently
    await asyncio.gather(
        bot_client.run_until_disconnected(),
        joiner_worker.run_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())
