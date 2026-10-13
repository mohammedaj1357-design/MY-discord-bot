"""Safe link previews and in-memory Discord image transformations."""

from __future__ import annotations

import asyncio
import io
import ipaddress
import math
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from discord.ext import commands
import discord

MAX_HTML_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 25_000_000
MAX_REDIRECTS = 3
MAX_TEXT = 6000
URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


# ═══════════════════════════════════════
# 🎨 الإعدادات الجديدة
# ═══════════════════════════════════════
IMAGE_HELP_EMBED = {
    "title": "🎨  أدوات تعديل الصور",
    "description": (
        "ارفع صورة مع الأمر أو أشر على صورة موجودة\n"
        "**الأوامر كلها تبدأ بـ** `!image` **أو** `!تعديل`"
    ),
    "color": 0x9B59B6,
    "commands": [
        ("🖼️ `resize`", "تغيير حجم الصورة\n`!image resize 800` أو `!image resize 800 600`"),
        ("✂️ `crop`", "قص الصورة\n`!image crop 800 600` أو `!image crop x1 y1 x2 y2`"),
        ("⚫ `grayscale`", "تحويل لأبيض وأسود\n`!image grayscale`"),
        ("🌫️ `blur`", "إضافة ضباب\n`!image blur 5`"),
        ("🔄 `rotate`", "تدوير الصورة\n`!image rotate 90`"),
        ("🎨 `invert`", "عكس الألوان\n`!image invert`"),
        ("🖼️ `border`", "إضافة حدود\n`!image border 20`"),
        ("✍️ `text`", "إضافة نص\n`!image text مرحبا بالعالم`"),
    ],
    "footer": "💡 الحد الأقصى: 8 MB  •  الحد الأقصى للأبعاد: 25 مليون بكسل",
}


def unique_urls(text: str, max_links: int = 2) -> list[str]:
    """Extract at most *max_links* normalized, unique HTTP(S) URLs."""
    found: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text or ""):
        value = match.group(0).rstrip(".,!?;:)]}'")
        if value not in seen:
            seen.add(value)
            found.append(value)
        if len(found) >= max(0, max_links):
            break
    return found


def validate_public_url(url: str) -> tuple[bool, str]:
    """Reject URLs that could be used for SSRF before DNS/network access."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False, "🔗 الرابط يجب أن يبدأ بـ http أو https."
        if parsed.username is not None or parsed.password is not None:
            return False, "🔒 الروابط التي تحتوي بيانات دخول غير مسموحة."
        host = parsed.hostname.rstrip(".")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return False, "🌐 هذا العنوان ليس عنواناً عاماً آمناً."
        if not address and host.lower() in {"localhost", "localhost.localdomain"}:
            return False, "🚫 الوصول إلى localhost غير مسموح."
        if parsed.port not in (None, 80, 443):
            return False, "🚫 منفذ الرابط غير مسموح."
    except (ValueError, UnicodeError):
        return False, "❌ الرابط غير صالح."
    return True, ""


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self._title_depth = 0
        self._ignored = 0
        self._in_meta = False
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._ignored += 1
        if tag == "title":
            self._title_depth += 1
        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            if name in {"description", "og:description"} and not self.description:
                self.description = (attrs.get("content") or "").strip()[:1000]

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._title_depth = max(0, self._title_depth - 1)
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._ignored = max(0, self._ignored - 1)

    def handle_data(self, data):
        if self._ignored:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._title_depth:
            self.title = (self.title + " " + text).strip()[:500]
        else:
            self._parts.append(text)

    def result(self) -> str:
        body = re.sub(r"\s+", " ", " ".join(self._parts)).strip()
        lines = ["📄 **[محتوى رابط خارجي غير موثوق]**", f"**العنوان:** {self.title or 'بدون عنوان'}"]
        if self.description:
            lines.append(f"**الوصف:** {self.description}")
        lines.append(f"**النص:** {body[:MAX_TEXT] or 'لم يُعثر على نص قابل للقراءة.'}")
        return "\n".join(lines)


def extract_html_context(html: str) -> str:
    parser = _ReadableHTML()
    parser.feed(html)
    parser.close()
    return parser.result()


class _PublicResolver(aiohttp.abc.AbstractResolver):
    """Resolver which only returns globally routable addresses."""

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, host, port, family, socket.SOCK_STREAM
            )
        except OSError as exc:
            raise OSError("DNS resolution failed") from exc
        records = []
        seen = set()
        for fam, _, _, _, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0])
            if not ip.is_global:
                raise OSError("DNS resolved to a non-public address")
            if (fam, str(ip)) in seen:
                continue
            seen.add((fam, str(ip)))
            records.append({"hostname": host, "host": str(ip), "port": port,
                            "family": fam, "proto": 0, "flags": 0})
        if not records:
            raise OSError("No public DNS address")
        return records

    async def close(self):
        return None


async def _fetch_public_html(url: str) -> tuple[str | None, str | None]:
    current = url
    resolver = _PublicResolver()
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
    try:
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
        ) as session:
            for _ in range(MAX_REDIRECTS + 1):
                valid, reason = validate_public_url(current)
                if not valid:
                    return None, reason
                try:
                    async with session.get(
                        current, allow_redirects=False,
                        headers={"Accept": "text/html,application/xhtml+xml"},
                    ) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location:
                                return None, "⚠️ الرابط أعاد تحويلاً بلا وجهة."
                            current = urljoin(current, location)
                            continue
                        if response.status >= 400:
                            return None, f"❌ تعذر الوصول للرابط (HTTP {response.status})."
                        content_type = response.headers.get("Content-Type", "").lower()
                        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                            return None, "📄 الرابط لا يحتوي صفحة HTML قابلة للقراءة."
                        length = int(response.headers.get("Content-Length", "0") or 0)
                        if length > MAX_HTML_BYTES:
                            return None, "⚠️ صفحة الرابط أكبر من الحد المسموح."
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.content.iter_chunked(16384):
                            total += len(chunk)
                            if total > MAX_HTML_BYTES:
                                return None, "⚠️ صفحة الرابط أكبر من الحد المسموح."
                            chunks.append(chunk)
                        raw = b"".join(chunks)
                        return raw.decode(response.charset or "utf-8", errors="replace"), None
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                    return None, "❌ تعذر الوصول للرابط حالياً."
            return None, "⚠️ عدد التحويلات في الرابط تجاوز الحد المسموح."
    finally:
        await resolver.close()


async def read_link_context(text: str, max_links: int = 2) -> str:
    """Read safe public HTML links and mark all returned text as untrusted."""
    urls = unique_urls(text, max_links)
    if not urls:
        return ""
    results = []
    for url in urls:
        valid, reason = validate_public_url(url)
        if not valid:
            results.append(f"🔗 {url}\n{reason}")
            continue
        html, error = await _fetch_public_html(url)
        results.append(f"🔗 {url}\n{error}" if error else f"🔗 {url}\n{extract_html_context(html or '')}")
    return "\n\n".join(results)


def parse_image_operation(operation: str, arguments: str = "") -> tuple[str, list[str]]:
    return operation.strip().lower(), arguments.split()


def transform_image(data: bytes, operation: str, args: list[str] | None = None) -> bytes:
    """Decode, transform, and encode an image entirely in memory."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("⚠️ حجم الصورة يتجاوز 8 ميغابايت.")
    args = args or []
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.verify()
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ValueError("⚠️ أبعاد الصورة تتجاوز 25 مليون بكسل.")
            image = ImageOps.exif_transpose(source).convert("RGBA")
    except Image.DecompressionBombError as exc:
        raise ValueError("⚠️ الصورة كبيرة أو خطرة للمعالجة.") from exc
    op = operation.lower()
    if op in {"resize", "حجم"}:
        if len(args) not in {1, 2}:
            raise ValueError("📝 الاستخدام: `resize العرض [الارتفاع]`")
        width = max(1, min(4096, int(args[0])))
        height = (
            max(1, min(4096, int(args[1])))
            if len(args) == 2
            else max(1, min(4096, round(image.height * width / image.width)))
        )
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    elif op in {"crop", "قص"}:
        if len(args) == 4:
            box = tuple(max(0, int(x)) for x in args)
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ValueError("❌ إحداثيات القص غير صالحة.")
            clipped = (
                min(box[0], image.width),
                min(box[1], image.height),
                min(box[2], image.width),
                min(box[3], image.height),
            )
            if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
                raise ValueError("❌ إحداثيات القص خارج حدود الصورة.")
            image = image.crop(clipped)
        elif len(args) == 2:
            width, height = (max(1, int(x)) for x in args)
            image = ImageOps.fit(image, (min(width, 4096), min(height, 4096)), centering=(.5, .5))
        else:
            raise ValueError("📝 الاستخدام: `crop العرض الارتفاع` أو `crop x1 y1 x2 y2`")
    elif op in {"gray", "grayscale", "رمادي"}:
        image = ImageOps.grayscale(image).convert("RGBA")
    elif op in {"blur", "ضباب"}:
        image = image.filter(ImageFilter.GaussianBlur(min(50, max(0, float(args[0]) if args else 3))))
    elif op in {"rotate", "تدوير"}:
        angle = float(args[0]) if args else 90
        radians = math.radians(angle)
        rotated_width = math.ceil(
            abs(image.width * math.cos(radians))
            + abs(image.height * math.sin(radians))
        )
        rotated_height = math.ceil(
            abs(image.width * math.sin(radians))
            + abs(image.height * math.cos(radians))
        )
        if rotated_width * rotated_height > MAX_PIXELS:
            raise ValueError("⚠️ أبعاد نتيجة الصورة تتجاوز 25 مليون بكسل.")
        image = image.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)
    elif op in {"invert", "عكس"}:
        alpha = image.getchannel("A")
        image = ImageOps.invert(image.convert("RGB")).convert("RGBA")
        image.putalpha(alpha)
    elif op in {"border", "حدود"}:
        size = max(1, min(200, int(args[0]) if args else 10))
        if (image.width + size * 2) * (image.height + size * 2) > MAX_PIXELS:
            raise ValueError("⚠️ أبعاد نتيجة الصورة تتجاوز 25 مليون بكسل.")
        image = ImageOps.expand(image, border=size, fill=(0, 0, 0, 255))
    elif op in {"text", "نص"}:
        if not args:
            raise ValueError("📝 الاستخدام: `text كلمة_أو_عبارة`")
        draw = ImageDraw.Draw(image)
        draw.text((20, 20), " ".join(args)[:200], fill=(255, 255, 255, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))
    else:
        raise ValueError("❌ عملية غير معروفة.")
    if image.width * image.height > MAX_PIXELS:
        raise ValueError("⚠️ أبعاد نتيجة الصورة تتجاوز 25 مليون بكسل.")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    if output.tell() > MAX_IMAGE_BYTES:
        raise ValueError("⚠️ نتيجة الصورة تتجاوز 8 ميغابايت.")
    return output.getvalue()


def build_image_help_embed() -> discord.Embed:
    """يبني embed أنيق لمساعدة الصور."""
    meta = IMAGE_HELP_EMBED
    embed = discord.Embed(
        title=meta["title"],
        description=meta["description"],
        color=meta["color"],
    )
    for name, description in meta["commands"]:
        embed.add_field(name=name, value=description, inline=False)
    embed.set_footer(text=meta["footer"])
    return embed


def setup_image_commands(bot) -> None:
    if getattr(bot, "_web_image_commands_registered", False):
        return

    @bot.command(name="image", aliases=["تعديل"])
    async def image_command(ctx, operation: str = None, *, arguments: str = ""):
        if not operation or operation.lower() in {"help", "مساعدة"}:
            await ctx.reply(
                embed=build_image_help_embed(),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        attachment = next(
            (a for a in ctx.message.attachments
             if (a.content_type or "").startswith("image/")
             or str(getattr(a, "filename", "")).lower().endswith(
                 (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"))),
            None,
        )
        if attachment is None and ctx.message.reference:
            try:
                referenced = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                attachment = next(
                    (a for a in referenced.attachments
                     if (a.content_type or "").startswith("image/")
                     or str(getattr(a, "filename", "")).lower().endswith(
                         (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"))),
                    None,
                )
            except (discord.HTTPException, discord.NotFound):
                attachment = None
        if attachment is None:
            await ctx.reply(
                "📎 **ارفع صورة مع الأمر** أو **أشر على رسالة تحتوي صورة**.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if int(getattr(attachment, "size", 0) or 0) > MAX_IMAGE_BYTES:
            await ctx.reply(
                "⚠️ **حجم الصورة يتجاوز 8 ميغابايت.**",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            result = transform_image(await attachment.read(), operation, arguments.split())
            await ctx.reply(
                file=discord.File(io.BytesIO(result), filename="edited.png"),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (ValueError, OSError, discord.HTTPException) as exc:
            await ctx.reply(
                f"❌ **تعذر تعديل الصورة:** {exc}",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    bot._web_image_commands_registered = True