"""نظام الطقس باستخدام Open-Meteo API."""
import aiohttp
import discord
from discord.ext import commands


async def get_coordinates(city: str):
    """جيب إحداثيات المدينة (يدعم العربي والإنجليزي)."""
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city, "count": 1, "language": "ar", "format": "json"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    results = data.get("results")
    if not results:
        return None

    return {
        "name": results[0]["name"],
        "country": results[0].get("country", ""),
        "lat": results[0]["latitude"],
        "lon": results[0]["longitude"],
    }


async def get_weather(lat: float, lon: float):
    """جيب حالة الطقس الحالية."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current_weather": "true",
        "hourly": "relativehumidity_2m",
        "timezone": "auto",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            return await resp.json()


def setup_weather(bot, developer_id):
    """تسجيل أوامر الطقس."""

    @bot.command(name="weather", aliases=["طقس"])
    async def weather_cmd(ctx, *, city: str = None):
        """عرض حالة الطقس."""
        if not city:
            await ctx.reply("⚠️ **الاستخدام:** `!weather مسقط`")
            return

        msg = await ctx.reply(f"🌤️ **جاري جلب طقس {city}...**")

        try:
            # جيب الإحداثيات
            coords = await get_coordinates(city)
            if not coords:
                await msg.edit(content=f"❌ ما لقيت مدينة **{city}**")
                return

            # جيب الطقس
            weather = await get_weather(coords["lat"], coords["lon"])
            if not weather or "current_weather" not in weather:
                await msg.edit(content=f"❌ تعذر جلب الطقس لـ **{city}**")
                return

            current = weather["current_weather"]
            humidity = weather["hourly"]["relativehumidity_2m"][0] if "hourly" in weather else "غير متاح"

            embed = discord.Embed(
                title=f"🌍 طقس {coords['name']}، {coords['country']}",
                color=0x4A90E2
            )
            embed.add_field(name="🌡️ الحرارة", value=f"`{current['temperature']}°C`", inline=True)
            embed.add_field(name="💨 الرياح", value=f"`{current['windspeed']} km/h`", inline=True)
            embed.add_field(name="💧 الرطوبة", value=f"`{humidity}%`", inline=True)

            await msg.edit(content=None, embed=embed)
        except Exception as e:
            await msg.edit(content=f"❌ خطأ: `{e}`")

    @bot.command(name="forecast", aliases=["توقعات"])
    async def forecast_cmd(ctx, *, city: str = None):
        """توقعات 3 أيام."""
        if not city:
            await ctx.reply("⚠️ **الاستخدام:** `!forecast مسقط`")
            return

        msg = await ctx.reply(f"🌤️ **جاري جلب توقعات {city}...**")

        try:
            coords = await get_coordinates(city)
            if not coords:
                await msg.edit(content=f"❌ ما لقيت مدينة **{city}**")
                return

            url = "https://api.open-meteo.com/v1/forecast"
            params = {
                "latitude": coords["lat"],
                "longitude": coords["lon"],
                "daily": "temperature_2m_max,temperature_2m_min,weathercode",
                "timezone": "auto",
                "forecast_days": 3,
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        await msg.edit(content=f"❌ تعذر جلب التوقعات")
                        return
                    data = await resp.json()

            daily = data["daily"]
            embed = discord.Embed(
                title=f"📅 توقعات 3 أيام - {coords['name']}",
                color=0x4A90E2
            )

            for i in range(3):
                date = daily["time"][i]
                max_t = daily["temperature_2m_max"][i]
                min_t = daily["temperature_2m_min"][i]
                embed.add_field(
                    name=f"📆 {date}",
                    value=f"🌡️ `{min_t}°C` → `{max_t}°C`",
                    inline=False
                )

            await msg.edit(content=None, embed=embed)
        except Exception as e:
            await msg.edit(content=f"❌ خطأ: `{e}`")

    print("✅ تم تحميل نظام الطقس (Open-Meteo)")
    return bot