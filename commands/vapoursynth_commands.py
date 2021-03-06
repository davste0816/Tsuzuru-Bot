from utils import HelperException
try:
    import vapoursynth
except ImportError:
    raise HelperException("VapourSynth is not available, stop importing all commands that need vapoursynth.")

import gc
import os
import argparse
import random
import tempfile
import asyncio
import logging
import discord
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot
from config import config
from utils import get_file
from functools import partial
from handle_messages import private_msg_file, private_msg, delete_user_message
from cmd_manager.decorators import register_command, add_argument

core = vapoursynth.core
core.add_cache = False
imwri = getattr(core, "imwri", getattr(core, "imwrif", None))
lossy = ["jpg", "jpeg", "gif"]


def get_upscaler(kernel=None, b=None, c=None, taps=None):
    upsizer = getattr(core.resize, kernel.title())
    if kernel == 'bicubic':
        upsizer = partial(upsizer, filter_param_a=b, filter_param_b=c)
    elif kernel == 'lanczos':
        upsizer = partial(upsizer, filter_param_a=taps)

    return upsizer


def get_descaler(kernel=None, b=None, c=None, taps=None):
    descale = getattr(core.descale_getnative, 'De' + kernel)
    if kernel == 'bicubic':
        descale = partial(descale, b=b, c=c)
    elif kernel == 'lanczos':
        descale = partial(descale, taps=taps)

    return descale


class DefineScaler:
    def __init__(self, kernel, b=None, c=None, taps=None):
        self.kernel = kernel
        self.b = b
        self.c = c
        self.taps = taps
        self.descaler = get_descaler(kernel=kernel, b=b, c=c, taps=taps)
        self.upscaler = get_upscaler(kernel=kernel, b=b, c=c, taps=taps)


scaler_dict = {
    "Bilinear": DefineScaler("bilinear"),
    "Bicubic (b=1/3, c=1/3)": DefineScaler("bicubic", b=1/3, c=1/3),
    "Bicubic (b=0.5, c=0)": DefineScaler(kernel="bicubic", b=.5, c=0),
    "Bicubic (b=0, c=0.5)": DefineScaler(kernel="bicubic", b=0, c=.5),
    "Bicubic (b=1, c=0)": DefineScaler(kernel="bicubic", b=1, c=0),
    "Bicubic (b=0, c=1)": DefineScaler(kernel="bicubic", b=0, c=1),
    "Bicubic (b=0.2, c=0.5)": DefineScaler(kernel="bicubic", b=.2, c=.5),
    "Lanczos (3 Taps)": DefineScaler(kernel="lanczos", taps=3),
    "Lanczos (4 Taps)": DefineScaler(kernel="lanczos", taps=4),
    "Lanczos (5 Taps)": DefineScaler(kernel="lanczos", taps=5),
    "Spline16": DefineScaler(kernel="spline16"),
    "Spline36": DefineScaler(kernel="spline36"),
    }


class GetNative:
    user_cooldown = set()

    def __init__(self, msg_author, img_url, fn, scaler, ar, min_h, max_h):
        self.msg_author = msg_author
        self.img_url = img_url
        self.filename = fn
        self.scaler = scaler
        self.ar = ar
        self.min_h = min_h
        self.max_h = max_h
        self.plotScaling = 'log'
        self.txt_output = ""
        self.resolutions = []
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.path = self.tmp_dir.name

    async def run(self):
        self.user_cooldown.add(self.msg_author)
        asyncio.get_event_loop().call_later(120, lambda: self.user_cooldown.discard(self.msg_author))

        image = await get_file(self.img_url, self.path, self.filename)
        if image is None:
            return True, "Can't load image. Pls try it again later."

        src = imwri.Read(image)
        if self.ar is 0:
            self.ar = src.width / src.height
        src_luma32 = convert_rgb_gray32(src)

        # descale each individual frame
        resizer = self.scaler.descaler
        upscaler = self.scaler.upscaler
        clip_list = []
        for h in range(self.min_h, self.max_h + 1):
            clip_list.append(resizer(src_luma32, getw(self.ar, h), h))
        full_clip = core.std.Splice(clip_list, mismatch=True)
        full_clip = upscaler(full_clip, getw(self.ar, src.height), src.height)
        if self.ar != src.width / src.height:
            src_luma32 = upscaler(src_luma32, getw(self.ar, src.height), src.height)
        expr_full = core.std.Expr([src_luma32 * full_clip.num_frames, full_clip], 'x y - abs dup 0.015 > swap 0 ?')
        full_clip = core.std.CropRel(expr_full, 5, 5, 5, 5)
        full_clip = core.std.PlaneStats(full_clip)
        full_clip = core.std.Cache(full_clip)

        tasks_pending = set()
        futures = {}
        vals = []
        for frame_index in range(len(full_clip)):
            fut = asyncio.ensure_future(asyncio.wrap_future(full_clip.get_frame_async(frame_index)))
            tasks_pending.add(fut)
            futures[fut] = frame_index
            while len(tasks_pending) >= core.num_threads / 2:  # let the bot not use 100% of the cpu
                tasks_done, tasks_pending = await asyncio.wait(
                    tasks_pending, return_when=asyncio.FIRST_COMPLETED)
                vals += [(futures.pop(task), task.result().props.PlaneStatsAverage) for task in tasks_done]

        tasks_done, _ = await asyncio.wait(tasks_pending)
        vals += [(futures.pop(task), task.result().props.PlaneStatsAverage) for task in tasks_done]
        vals = [v for _, v in sorted(vals)]
        ratios, vals, best_value = self.analyze_results(vals)
        self.save_plot(vals)
        self.txt_output += 'Raw data:\nResolution\t | Relative Error\t | Relative difference from last\n'
        for i, error in enumerate(vals):
            self.txt_output += f'{i + self.min_h:4d}\t\t | {error:.10f}\t\t\t | {ratios[i]:.2f}\n'

        with open(f"{self.path}/{self.filename}.txt", "w") as file_open:
            file_open.writelines(self.txt_output)

        return False, best_value

    def analyze_results(self, vals):
        ratios = [0.0]
        for i in range(1, len(vals)):
            last = vals[i - 1]
            current = vals[i]
            ratios.append(current and last / current)
        sorted_array = sorted(ratios, reverse=True)  # make a copy of the array because we need the unsorted array later
        max_difference = sorted_array[0]

        differences = [s for s in sorted_array if s - 1 > (max_difference - 1) * 0.33][:5]

        for diff in differences:
            current = ratios.index(diff)
            # don't allow results within 20px of each other
            for res in self.resolutions:
                if res - 20 < current < res + 20:
                    break
            else:
                self.resolutions.append(current)

        scaler = self.scaler
        bicubic_params = scaler.kernel == 'bicubic' and f'Scaling parameters:\nb = {scaler.b:.2f}\nc = {scaler.c:.2f}\n' or ''
        best_values = f"{'p, '.join([str(r + self.min_h) for r in self.resolutions])}p"
        self.txt_output += f"Resize Kernel: {scaler.kernel}\n{bicubic_params}Native resolution(s) (best guess): " \
                           f"{best_values}\nPlease check the graph manually for more accurate results\n\n"

        return ratios, vals, f"Native resolution(s) (best guess): {best_values}"

    def save_plot(self, vals):
        matplotlib.pyplot.style.use('dark_background')
        matplotlib.pyplot.plot(range(self.min_h, self.max_h + 1), vals, '.w-')
        matplotlib.pyplot.title(self.filename)
        matplotlib.pyplot.ylabel('Relative error')
        matplotlib.pyplot.xlabel('Resolution')
        matplotlib.pyplot.yscale(self.plotScaling)
        matplotlib.pyplot.savefig(f'{self.path}/{self.filename}.png')
        matplotlib.pyplot.clf()


class GetScaler:
    user_cooldown = set()

    def __init__(self, msg_author, img_url, fn, native_height):
        self.msg_author = msg_author
        self.img_url = img_url
        self.filename = fn
        self.native_height = native_height
        self.plotScaling = 'log'
        self.ar = None
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.path = self.tmp_dir.name

    async def run(self):
        self.user_cooldown.add(self.msg_author)
        asyncio.get_event_loop().call_later(120, lambda: self.user_cooldown.discard(self.msg_author))

        image = await get_file(self.img_url, self.path, self.filename)
        if image is None:
            return True, "Can't load image. Pls try it again later."

        src = imwri.Read(image, float_output=True)
        self.ar = src.width / src.height
        src_luma32 = convert_rgb_gray32(src)

        results_bin = {}
        for name, scaler in scaler_dict.items():
            error = self.geterror(src_luma32, self.native_height, scaler)
            results_bin[name] = error

        sorted_results = list(sorted(results_bin.items(), key=lambda x: x[1]))
        best_result = sorted_results[0]
        longest_key = max(map(len, results_bin))

        try:
            txt_output = "\n".join(f"{scaler_name:{longest_key}}  "
                                   f"{0 if best_result[1] == 0.0 else value / best_result[1]:7.1%}  "
                                   f"{value:.10f}" for scaler_name, value in sorted_results)
        except ZeroDivisionError:
            txt_output = "Broken Ouput!" + "\n".join(f"{scaler_name:{longest_key}}  {best_result[1]:7.1%}"
                                                     f"  {value:.10f}" for scaler_name, value in sorted_results)

        for name, scaler in scaler_dict.items():
            if name == best_result[0]:
                self.save_images(src, scaler, self.native_height)

        end_text = f"Testing scalers for native height: {self.native_height}\n```{txt_output}```\n" \
                   f"Smallest error achieved by \"{best_result[0]}\" ({best_result[1]:.10f})"

        return False, end_text

    def geterror(self, clip, h, scaler):
        down = scaler.descaler(clip, getw(self.ar, h), h)
        up = scaler.upscaler(down, getw(self.ar, clip.height), clip.height)
        smask = core.std.Expr([clip, up], 'x y - abs dup 0.015 > swap 0 ?')
        smask = core.std.CropRel(smask, 5, 5, 5, 5)
        mask = core.std.PlaneStats(smask)
        luma = mask.get_frame(0).props.PlaneStatsAverage
        return luma

    def save_images(self, src_luma32, scaler, h):
        src = src_luma32
        src = scaler.descaler(src, getw(self.ar, h), h)
        first_out = imwri.Write(src, 'png', f'{self.path}/{self.filename}_source%d.png')
        first_out.get_frame(0)  # trick vapoursynth into rendering the frame


class Grain:
    user_cooldown = set()

    def __init__(self, msg_author, img_url, filename):
        self.img_url = img_url
        self.msg_author = msg_author
        self.filename = filename
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.path = self.tmp_dir.name

    async def run(self):
        self.user_cooldown.add(self.msg_author)
        asyncio.get_event_loop().call_later(120, lambda: self.user_cooldown.discard(self.msg_author))

        image = await get_file(self.img_url, self.path, self.filename)
        if image is None:
            return True, "Can't load image. Pls try it again later."

        src = imwri.Read(image)
        var = random.randint(100, 2000)
        hcorr = random.uniform(0.0, 1.0)
        vcorr = random.uniform(0.0, 1.0)
        src = core.grain.Add(src, var=var, hcorr=hcorr, vcorr=vcorr)
        first_out = imwri.Write(src, 'png', f'{self.path}/{self.filename}_grain%d.png')
        first_out.get_frame(0)  # trick vapoursynth into rendering the frame

        return False, f"var: {var}, hcorr: {hcorr}, vcorr: {vcorr}"


def convert_rgb_gray32(src):
    matrix_s = '709' if src.format.color_family == vapoursynth.RGB else None
    src_luma32 = core.resize.Point(src, format=vapoursynth.YUV444PS, matrix_s=matrix_s)
    src_luma32 = core.std.ShufflePlanes(src_luma32, 0, vapoursynth.GRAY)
    src_luma32 = core.std.Cache(src_luma32)
    return src_luma32


def getw(ar, h, only_even=True):
    w = h * ar
    w = int(round(w))
    if only_even:
        w = w // 2 * 2

    return w


def to_float(str_value):
    if set(str_value) - set("0123456789./"):
        raise argparse.ArgumentTypeError("Invalid characters in float parameter")
    try:
        return eval(str_value) if "/" in str_value else float(str_value)
    except (SyntaxError, ZeroDivisionError, TypeError, ValueError):
        raise argparse.ArgumentTypeError("Exception while parsing float") from None


async def check_message(message):
    if not message.attachments:
        await private_msg(message, "Picture as attachment is needed.")
    elif not message.attachments[0].width:
        await private_msg(message, "Filetype is not allowed!")
    elif message.attachments[0].width * message.attachments[0].height > 8300000:
        await private_msg(message, "Picture is too big.")
    else:
        return True

    await delete_user_message(message)
    return False


@register_command('getnative', description='Find the native resolution(s) of upscaled material (mostly anime)')
@add_argument('--aspect-ratio', '-ar', dest='ar', type=to_float, default=0, help='Force aspect ratio. Only useful for anamorphic input')
@add_argument('--min-height', '-min', dest="min_h", type=int, default=500, help='Minimum height to consider')
@add_argument('--max-height', '-max', dest="max_h", type=int, default=1000, help='Maximum height to consider [max 1080 atm]')
@add_argument('--scaler', '-s', dest='scaler', type=str, default='Bicubic (b=1/3, c=1/3)', help='Use a predefined scaler.')
@add_argument('--kernel', '-k', dest='kernel', type=str.lower, default=None, help='Resize kernel to be used')
@add_argument('--bicubic-b', '-b', dest='b', type=to_float, default="1/3", help='B parameter of bicubic resize')
@add_argument('--bicubic-c', '-c', dest='c', type=to_float, default="1/3", help='C parameter of bicubic resize')
@add_argument('--lanczos-taps', '-t', dest='taps', type=int, default=3, help='Taps parameter of lanczos resize')
async def getnative(client, message, args):
    if not await check_message(message):
        return

    if message.author.id in GetNative.user_cooldown:
        return await private_msg(message, "Pls use this command only every 2min.")
    elif os.path.splitext(message.attachments[0].filename)[1][1:] in lossy:
        return await private_msg(message, f"No lossy format pls. Lossy formats are:\n{', '.join(lossy)}")
    elif args.min_h >= message.attachments[0].height:
        return await private_msg(message, f"Picture is to small or equal for min height {args.min_h}.")
    elif args.min_h >= args.max_h:
        return await private_msg(message, f"Your min height is bigger or equal to max height.")
    elif args.max_h - args.min_h > 1000:
        return await private_msg(message, f"Max - min height bigger than 1000 is not allowed")
    elif args.max_h > message.attachments[0].height:
        await private_msg(message, f"Your max height cant be bigger than your image dimensions. New max height is {message.attachments[0].height}")
        args.max_h = message.attachments[0].height

    if args.kernel is None:
        if args.scaler not in scaler_dict.keys():
            return await private_msg(message, f'Scaler is not a defined, pls use ">>showscaler".')
        scaler = scaler_dict[args.scaler]
    else:
        if args.kernel not in ['spline36', 'spline16', 'lanczos', 'bicubic', 'bilinear']:
            return await private_msg(message, f'descale: {args.kernel} is not a supported kernel.')
        scaler = DefineScaler(args.kernel, b=args.b, c=args.c, taps=args.taps)

    delete_message = await message.channel.send(file=discord.File(config.PICTURE.spam + "tenor_loading.gif"))

    msg_author = message.author.id
    img_url = message.attachments[0].url
    filename = message.attachments[0].filename
    getn = GetNative(msg_author, img_url, filename, scaler, args.ar, args.min_h, args.max_h)
    try:
        import time
        starttime = time.time()
        forbidden_error, best_value = await getn.run()
        print(time.time() - starttime)
    except BaseException as err:
        forbidden_error = True
        best_value = "Error in getnative, can't process your picture."
        logging.info(f"Error in getnative: {err}")
    gc.collect()

    if not forbidden_error:
        content = ''.join([
        f"Output:"
        f"\nKernel: {scaler.kernel} ",
        f"AR: {getn.ar:.2f} ",
        f"B: {scaler.b:.2f} C: {scaler.c:.2f} " if scaler.kernel == "bicubic" else "",
        f"Taps: {scaler.taps} " if scaler.kernel == "lanczos" else "",
        f"\n{best_value}",
        ])
        await private_msg_file(message, f"{getn.path}/{filename}.txt", "Output from getnative.")
        await message.channel.send(file=discord.File(f'{getn.path}/{filename}'), content=f"Input\n{message.author}: \"{message.content}\"")
        await message.channel.send(file=discord.File(f'{getn.path}/{filename}.png'), content=content)
    else:
        await private_msg(message, best_value)

    await delete_user_message(message)
    await delete_user_message(delete_message)
    getn.tmp_dir.cleanup()


@register_command('getscaler', description='Find the best inverse scaler (mostly anime)')
@add_argument("--native_height", "-nh", dest="native_height", type=int, default=720, help="Approximated native height. Default is 720")
async def getscaler(client, message, args):
    if not await check_message(message):
        return

    if message.author.id in GetScaler.user_cooldown:
        return await private_msg(message, "Pls use this command only every 2min.")
    elif os.path.splitext(message.attachments[0].filename)[1][1:] in lossy:
        return await private_msg_file(message, config.PICTURE.spam + "lossy.png", content=f"No lossy format pls. Lossy formats are:\n{', '.join(lossy)}")

    delete_message = await message.channel.send(file=discord.File(config.PICTURE.spam + "tenor_loading.gif"))

    msg_author = message.author.id
    img_url = message.attachments[0].url
    filename = message.attachments[0].filename
    gets = GetScaler(msg_author, img_url, filename, args.native_height)
    try:
        forbidden_error, best_value = await gets.run()
    except BaseException as err:
        forbidden_error = True
        best_value = "Error in getscaler, can't process your picture."
        logging.info(f"Error in getscaler: {err}")
    gc.collect()

    if not forbidden_error:
        await message.channel.send(file=discord.File(f'{gets.path}/{filename}'), content=f"Input\n{message.author}: \"{message.content}\"")
        await message.channel.send(file=discord.File(f'{gets.path}/{filename}_source0.png'), content=f"Output\n{best_value}")
    else:
        await private_msg(message, best_value)

    await delete_user_message(message)
    await delete_user_message(delete_message)
    gets.tmp_dir.cleanup()


@register_command('grain', description='Grain.')
async def grain(client, message, args):
    if not await check_message(message):
        return

    if message.author.id in Grain.user_cooldown:
        return await private_msg(message, "Pls use this command only every 2min.")

    delete_message = await message.channel.send(file=discord.File(config.PICTURE.spam + "tenor_loading.gif"))

    msg_author = message.author.id
    img_url = message.attachments[0].url
    filename = message.attachments[0].filename
    gra = Grain(msg_author, img_url, filename)
    try:
        forbidden_error, best_value = await gra.run()
    except BaseException as err:
        forbidden_error = True
        best_value = "Error in Grain, can't process your picture."
        logging.info(f"Error in grain: {err}")
    gc.collect()

    if not forbidden_error:
        try:
            await message.channel.send(file=discord.File(f'{gra.path}/{filename}_grain0.png'), content=f"Grain <:diGG:302631286118285313>\n{best_value}")
        except discord.HTTPException:
            await message.channel.send("Too much grain <:notlikemiya:328621519037005826>")
    else:
        await private_msg(message, best_value)

    await delete_user_message(message)
    await delete_user_message(delete_message)
    gra.tmp_dir.cleanup()


@register_command('showscaler', description='Show all available scaler.')
async def showscaler(client, message, args):
    content = ",\n".join(scaler_dict.keys())
    await delete_user_message(message)
    await private_msg(message, content)
