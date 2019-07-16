import aiohttp
import discord
import asyncio
import random
import logging
from handle_messages import private_msg_user, delete_user_message


prison_inmates = {}
user_roles = {}
user_cooldown = set()


def get_role_by_id(server, role_id):
    for role in server.roles:
        if role.id == role_id:
            return role
    return None


def has_role(user, role_id):
    return get_role_by_id(user, role_id) is not None


async def get_file(url, path, filename, message=None):
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url) as resp:
            if resp.status != 200:
                return None
            if message is not None:
                await delete_user_message(message)
            with open(f"{path}/{filename}", 'wb') as f:
                f.write(await resp.read())
            return f"{path}/{filename}"


async def delete_role(user, role):
    while prison_inmates[user.id] > 0:
        await asyncio.sleep(60)
        prison_inmates[user.id] -= 1

    prison_inmates.pop(user.id)
    try:
        await user.remove_roles(role)
    except (discord.Forbidden, discord.HTTPException):
        return logging.error("Can't remove user roles")
    try:
        await user.edit(roles=user_roles[user.id])
    except (discord.Forbidden, discord.HTTPException):
        return logging.error("Can't add user roles")
    except KeyError:
        return logging.error(f"KeyError for user: {user}")

    user_roles.pop(user.id)


async def punish_user(client, message, user=None, reason="Stop using this command!", prison_length=None):
    not_in_prison = True
    if message.author.id in user_roles or message.author.id in prison_inmates:
        return await message.channel.send(f"User in prison can't use this command!")

    if prison_length is None:
        prison_length = random.randint(30, 230)

    user = user or message.author
    if user.id in prison_inmates:
        not_in_prison = False
        if prison_length == 0:
            prison_inmates[user.id] = 0
        else:
            prison_inmates[user.id] += prison_length
    else:
        prison_inmates[user.id] = prison_length
        user_roles[user.id] = user.roles[1:]
        await user.edit(roles=[], reason="Ultimate Prison")
        role = get_role_by_id(message.guild, 451076667377582110)
        await user.add_roles(role)
        asyncio.ensure_future(delete_role(user, role))

    await send_log_message(client, f"Username: {user.name}\nNew Time: {prison_length}min\nFull Time: "
                                   f"{str(prison_inmates[user.id]) + 'min' if prison_length > 0 else 'Reset'}"
                                   f"\nReason: {reason}\nBy: {message.author.name}")
    await private_msg_user(message, f"{'Prison is now active' if not_in_prison else 'New Time:'}\nTime: "
                                    f"{prison_inmates[user.id]}min\nReason: {reason}", user)


async def send_log_message(client, message):
    channel = client.get_channel(246368272327507979)
    await channel.send(message)


def set_user_cooldown(author, time):
    user_cooldown.add(author)
    asyncio.get_event_loop().call_later(time, lambda: user_cooldown.discard(author))
