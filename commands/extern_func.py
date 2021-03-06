import json
import discord
import aiohttp
import urllib.parse
from dictcc import Dict, AVAILABLE_LANGUAGES
from config import config
from collections import defaultdict
from cmd_manager.decorators import register_command, add_argument
from merriam_api import CollegiateDictionary, WordNotFoundException
import duckduckgo

coll_key = config.MAIN.coll_key


async def lookup_jisho(query):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://jisho.org/api/v1/search/words?keyword={query}") as response:
            if response.status != 200:
                return None
            data = json.loads(await response.text())

    return data['data']


def run_dict(word, inlang, outlang):
    result = Dict.translate(word, inlang, outlang)
    return result.translation_tuples


def lookup_merriam(query):
    dictionary = CollegiateDictionary(coll_key)
    print(dictionary)
    defs = defaultdict(list)
    try:
        for entry in dictionary.lookup(query):
            for definition, _ in entry.senses:
                defs[entry.function].append(definition)
    except WordNotFoundException:
        defs = {}
    return defs


@register_command('ddg', description='Search a keyword with duckduckgo')
@add_argument('query', help='Your search query. Can use bangs, e.g. `!unicode EXCLAMATION MARK`.')
async def ddg(client, message, args):
    # https://github.com/strinking/python-duckduckgo
    # https://duckduckgo.com/api
    answer = await duckduckgo.query(args.query, safesearch=False)
    if answer.type == 'exclusive' and answer.redirect.url:
        # Let discord build the embed for the redirect
        await message.channel.send(f"Redirected to: {answer.redirect.url}")
        return

    embed = discord.Embed(title=answer.heading)
    embed.set_author(name="DuckDuckGo Instant Answer", icon_url="https://duckduckgo.com/favicon.png")
    if answer.type == 'nothing' or answer.type == 'name' and not answer.abstract.text:
        embed.description = "No results."
    else:
        if answer.answer.text:
            embed.add_field(name=f"Answer ({answer.answer.type})", value=str(answer.answer.text))
        if answer.abstract.text or answer.abstract.url:
            abs_ = answer.abstract
            embed.add_field(name="Abstract", value=f"{abs_.text} (<{abs_.url}>; {abs_.source})")
            if answer.image.url:
                embed.set_image(url=answer.image.url)
        if answer.results:
            for result in answer.results[:2]:
                embed.add_field(name="Result", value=f"{result.text} (<{result.url}>)")
                if result.icon.url and not embed.thumbnail:
                    embed.set_thumbnail(url=result.icon.url)
        if answer.related:
            for result in answer.related[:2]:
                if result.topics:
                    result = result.topics[0]  # just pick the first here
                embed.add_field(name="Related", value=f"{result.text} (<{result.url}>)")
                if result.icon.url and not embed.thumbnail:
                    embed.set_thumbnail(url=result.icon.url)
        if answer.definition.text:
            def_ = answer.definition
            embed.add_field(name="Definition", value=f"{def_.text} (<{def_.url}>; {def_.source})")

    await message.channel.send(embed=embed)


@register_command('jisho', description='Translate a keyword with jisho.')
@add_argument('keyword', help='Keyword for translation.')
async def jisho(client, message, args):
    result_list = await lookup_jisho(args.keyword)
    if not result_list:
        return await message.channel.send('Nothing Found')

    quote = urllib.parse.quote(args.keyword)
    embed = discord.Embed(title=f"Search for '{args.keyword}'", description="")
    embed.set_author(name="Master Jisho", url=f'http://jisho.org/search/{quote}')
    for result in result_list[:4]:
        jap = result['japanese'][:3]
        jap_words = [item.get('word', item.get('reading', '-')) for item in jap]
        jap_readings = [item.get('reading', '-') for item in jap]
        senses = result['senses'][:3]
        eng_meanings = []
        for sense in senses:
            eng_meanings.extend(sense['english_definitions'][:2])

        text = f"*Reading*: {'、'.join(jap_readings)}\n*Meaning*: {', '.join(eng_meanings)}"
        embed.add_field(name="、".join(jap_words), value=text, inline=False)
    await message.channel.send(embed=embed)


@register_command('define', description='Define a word with merriam.')
@add_argument('keyword', help='Keyword for defination.')
@add_argument('--type', '-t', help="Only show definitions for this word type.")
async def merriam(client, message, args):
    defs = lookup_merriam(args.keyword)
    if not defs:
        return await message.channel.send('Nothing Found')

    quote = urllib.parse.quote(args.keyword)
    embed = discord.Embed(title=f"Search for '{args.keyword}'", description="")
    embed.set_author(name="Master Merriam", url=f'https://www.merriam-webster.com/dictionary/{quote}')

    word_types = defs.keys() if not args.type else {args.type}
    entries_per_type = max(5 // len(word_types), 1)

    for word_type in word_types:
        descriptions = defs[word_type]
        lines = [f"- {l}" for l in descriptions[:entries_per_type]]
        text = "\n".join(lines)
        if len(text) > 2000:
            text = text[:2000] + "…"
        embed.add_field(name=f"[{word_type}]", value=text, inline=False)

    await message.channel.send(embed=embed)


@register_command('dict', description='Dict will show you translation for your input/output language.')
@add_argument('keyword', help="Keyword for translation.")
@add_argument('--in-lang', '-i', default="de", choices=AVAILABLE_LANGUAGES.keys(), help='Input language.')
@add_argument('--out-lang', '-o', default="en", choices=AVAILABLE_LANGUAGES.keys(), help="Output language.")
async def dict_cc(client, message, args):
    trans_tuples = run_dict(args.keyword, args.in_lang, args.out_lang)

    if not trans_tuples:
        return await message.channel.send('Nothing Found')

    quote = urllib.parse.quote(args.keyword)
    embed = discord.Embed(title=f"Search for '{args.keyword}' ({args.in_lang} ⇔ {args.out_lang})", description="")
    embed.set_author(name="Master Dict", url=f'https://www.dict.cc/?s={quote}')
    for in_word, out_word in trans_tuples[:6]:
        embed.add_field(name=in_word, value=out_word, inline=True)
    await message.channel.send(embed=embed)
