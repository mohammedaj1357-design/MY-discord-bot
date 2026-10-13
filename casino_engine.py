"""Synchronous, atomic virtual-credit casino rules.

All credit changes and persistent cooldown updates happen inside one GameStore
transaction. A positive game result is *net profit*, not a stake-inclusive payout.
"""

import math
import random
import time

MAX_STAKE = 1_000_000_000_000
RED_NUMBERS = frozenset((1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23,
                         25, 27, 30, 32, 34, 36))
SYMBOLS = ("🍒", "🍋", "🍊", "🍇", "⭐", "💎", "7️⃣")


class CasinoError(ValueError):
    """User-facing invalid bet, insufficient funds, or cooldown."""


def positive_amount(value):
    try:
        if isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal():
            raise ValueError
        number = int(value)
    except (TypeError, ValueError):
        raise CasinoError("❌ أدخل مبلغاً صحيحاً موجباً.") from None
    if number < 1 or number > MAX_STAKE:
        raise CasinoError(f"❌ المبلغ يجب أن يكون بين 1 و {MAX_STAKE:,} رصيد افتراضي.")
    return number


def selection(value, low, high):
    try:
        if not str(value).isascii() or not str(value).isdecimal():
            raise ValueError
        number = int(value)
    except (TypeError, ValueError):
        raise CasinoError(f"❌ اختر رقماً من {low} إلى {high}.") from None
    if not low <= number <= high:
        raise CasinoError(f"❌ اختر رقماً من {low} إلى {high}.")
    return number


def roulette_choice(choice, number=None):
    choice = str(choice or "").strip().lower()
    colors = {"red": "أحمر", "احمر": "أحمر", "أحمر": "أحمر",
              "black": "أسود", "اسود": "أسود", "أسود": "أسود",
              "green": "أخضر", "اخضر": "أخضر", "أخضر": "أخضر"}
    if choice in colors and number is None:
        return ("color", colors[choice])
    if choice in ("رقم", "number") and number is not None:
        return ("number", selection(number, 0, 36))
    if number is None and choice.isascii() and choice.isdecimal():
        return ("number", selection(choice, 0, 36))
    raise CasinoError("❌ الاستخدام: !روليت <مبلغ> أحمر/أسود/أخضر أو !روليت <مبلغ> رقم <0-36>.")


def _cooldown(data, uid, name, seconds, now):
    key = f"{uid}_{name}"
    cooldowns = data.setdefault("cooldowns", {})
    if key in cooldowns:
        remaining = seconds - (now - float(cooldowns[key]))
        if remaining > 0:
            raise CasinoError(f"⏰ انتظر {math.ceil(remaining)} ثانية قبل إعادة المحاولة.")
    cooldowns[key] = now


def _afford(user, amount):
    if user["money"] < amount:
        raise CasinoError(f"❌ رصيد غير كافٍ! رصيدك: {user['money']:,} رصيد افتراضي.")


def _result(game, amount, rng):
    """Return (net change, public description). Called only after affordability check."""
    if game == "mine":
        chosen = (selection(rng["number"], 1, 6)
                  if rng.get("number") is not None else random.randint(1, 6))
        rolled = random.randint(1, 6)
        return (amount * 5 if chosen == rolled else -amount,
                f"🎲 رقمك: {chosen} | رقم النرد: {rolled}")
    if game == "treasure":
        roll = random.random()
        box = random.randint(1, 3)
        return (amount * 3 if roll < .3 else amount * 3 // 2 if roll < .6 else -amount,
                f"💎 الصندوق {box}: {'كنز كبير' if roll < .3 else 'كنز صغير' if roll < .6 else 'فارغ'}")
    if game == "towers":
        floors = 0
        while floors < 5 and random.random() >= .25:
            floors += 1
        return (amount * floors // 2 if floors else -amount,
                f"🏗️ صعدت {floors} طوابق (المضاعف x{1 + floors / 2:g})")
    if game == "rocket":
        point = random.uniform(1.1, 10.0)
        return (int(amount * (point - 1)) if point >= 2 else -amount,
                f"🚀 انفجر الصاروخ عند {point:.2f}x (الفوز إذا وصل 2x)")
    if game == "invest":
        roll = random.random()
        if roll < .6:
            percent = random.uniform(.1, .5)
            delta = int(amount * percent)
        elif roll < .85:
            percent, delta = 0, 0
        else:
            percent = random.uniform(.05, .3)
            delta = -int(amount * percent)
        return delta, f"📈 تغير الاستثمار: {percent * 100:.1f}% {'ربح' if delta > 0 else 'خسارة' if delta < 0 else 'تعادل'}"
    if game == "trade":
        roll = random.random()
        if roll < .35:
            delta = int(amount * (random.uniform(1.5, 3.0) - 1))
        elif roll < .5:
            delta = 0
        else:
            delta = -int(amount * random.uniform(.2, .8))
        return delta, "📊 صفقة تداول"
    if game == "slots":
        reels = [random.choice(SYMBOLS) for _ in range(3)]
        if len(set(reels)) == 1:
            delta = amount * {"7️⃣": 10, "💎": 7, "⭐": 5}.get(reels[0], 3)
        elif len(set(reels)) == 2:
            delta = amount * 3 // 2
        else:
            delta = -amount
        return delta, "🎰 " + " | ".join(reels)
    if game == "wheel":
        names = ("خسارة", "تعادل", "x2", "x3", "x5", "x10")
        multiplier = random.choices((0, 1, 2, 3, 5, 10),
                                     weights=(30, 20, 25, 15, 8, 2))[0]
        return (-amount if multiplier == 0 else 0 if multiplier == 1 else amount * multiplier,
                f"🎡 العجلة: {names[(0, 1, 2, 3, 5, 10).index(multiplier)]}")
    if game == "roulette":
        kind, picked = rng["choice"]
        landed = random.randint(0, 36)
        color = "أخضر" if landed == 0 else "أحمر" if landed in RED_NUMBERS else "أسود"
        won = (picked == landed if kind == "number" else picked == color)
        # Reference values describe net winnings, not total stake-inclusive payouts.
        multiplier = 35 if kind == "number" else 14 if picked == "أخضر" else 2
        return (amount * multiplier if won else -amount,
                f"🎰 الروليت: {landed} ({color}) | اخترت {picked}")
    if game == "blackjack":
        cards = (2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 11)
        player = [random.choice(cards), random.choice(cards)]
        dealer = [random.choice(cards), random.choice(cards)]
        # Ace counts 1 if counting it as 11 would cause a bust.
        def total(hand):
            value = sum(hand)
            while value > 21 and 11 in hand:
                hand = hand.copy()
                hand[hand.index(11)] = 1
                value = sum(hand)
            return value
        p, d = total(player), total(dealer)
        if p == 21 and d == 21:
            delta = 0
        elif p == 21:
            delta = amount * 5 // 2
        elif d == 21:
            delta = -amount
        else:
            delta = amount * 2 if p > d else 0 if p == d else -amount
        return delta, f"🃏 أوراقك {player} = {p} | الموزع {dealer} = {d}"
    if game == "race":
        horse = selection(rng["horse"], 1, 5)
        winner = random.choices((1, 2, 3, 4, 5), weights=(30, 25, 20, 15, 10))[0]
        return (amount * (6 - horse) if winner == horse else -amount,
                f"🐎 حصانك: {horse} | الفائز: {winner}")
    if game == "shoot":
        label, multiplier = random.choices(
            (("🎯 في المنتصف", 3), ("🎯 قريب جداً", 2),
             ("🎯 قريب", 1.5), ("❌ بعيد", 0)), weights=(15, 25, 30, 30))[0]
        return (int(amount * multiplier) if multiplier else -amount, label)
    raise ValueError(f"Unknown casino game: {game}")


COOLDOWNS = {"invest": 3600, "trade": 1800}


def play(store, uid, game, stake, *, now=None, **options):
    amount = positive_amount(stake)
    if game == "roulette":
        options["choice"] = roulette_choice(options.get("choice"), options.get("number"))
    elif game == "mine" and options.get("number") is not None:
        selection(options.get("number"), 1, 6)
    elif game == "race":
        selection(options.get("horse"), 1, 5)
    if game not in ("mine", "treasure", "towers", "rocket", "invest", "trade",
                    "slots", "wheel", "roulette", "blackjack", "race", "shoot"):
        raise CasinoError("❌ لعبة غير معروفة.")
    with store.transaction() as data:
        user = store.user(data, uid)
        _afford(user, amount)
        if game in COOLDOWNS:
            _cooldown(data, uid, game, COOLDOWNS[game], time.time() if now is None else now)
        delta, detail = _result(game, amount, options)
        user["money"] += delta
        balance = user["money"]
    return delta, detail, balance


def reward(store, uid, kind, *, now=None):
    if kind not in ("salary", "bakshish"):
        raise CasinoError("❌ مكافأة غير معروفة.")
    with store.transaction() as data:
        _cooldown(data, uid, kind, 86400 if kind == "salary" else 300,
                  time.time() if now is None else now)
        if kind == "salary":
            amount = random.randint(500, 2000)
        else:
            roll = random.random()
            amount = (random.randint(1000, 5000) if roll < .05 else
                      random.randint(100, 500) if roll < .30 else
                      random.randint(10, 100) if roll < .70 else 0)
        user = store.user(data, uid)
        user["money"] += amount
        balance = user["money"]
    return amount, balance


def balance(store, uid):
    with store.transaction() as data:
        return store.user(data, uid)["money"]


def transfer(store, sender, recipient, stake):
    amount = positive_amount(stake)
    if sender == recipient:
        raise CasinoError("❌ لا يمكنك التحويل لنفسك.")
    with store.transaction() as data:
        source = store.user(data, sender)
        _afford(source, amount)
        target = store.user(data, recipient)
        source["money"] -= amount
        target["money"] += amount
        remaining = source["money"]
    return remaining


def buy_ticket(store, uid, stake):
    amount = positive_amount(stake)
    with store.transaction() as data:
        user = store.user(data, uid)
        _afford(user, amount)
        lottery = data.setdefault("lottery", {"tickets": {}, "pot": 0})
        tickets = lottery.setdefault("tickets", {})
        user["money"] -= amount
        tickets[str(uid)] = tickets.get(str(uid), 0) + amount
        lottery["pot"] = lottery.get("pot", 0) + amount
        pot = lottery["pot"]
    return pot


def draw_lottery(store):
    with store.transaction() as data:
        lottery = data.setdefault("lottery", {"tickets": {}, "pot": 0})
        tickets = lottery.get("tickets", {})
        if not tickets:
            raise CasinoError("❌ لا توجد تذاكر للسحب.")
        winner = random.choices(list(tickets), weights=list(tickets.values()))[0]
        pot = lottery["pot"]
        store.user(data, int(winner))["money"] += pot
        lottery["tickets"] = {}
        lottery["pot"] = 0
    return int(winner), pot


def lottery_status(store):
    with store.transaction() as data:
        lottery = data.setdefault("lottery", {"tickets": {}, "pot": 0})
        return lottery["pot"], sum(lottery["tickets"].values())


def settle_challenge(store, challenger, opponent, stake):
    """Called only after the target explicitly accepts. Rechecks both balances atomically."""
    amount = positive_amount(stake)
    if challenger == opponent:
        raise CasinoError("❌ لا يمكنك تحدي نفسك.")
    with store.transaction() as data:
        blocked = data.get("blocked_users", [])
        blocked_ids = {str(uid) for uid in (blocked.keys() if isinstance(blocked, dict) else blocked)}
        if str(challenger) in blocked_ids or str(opponent) in blocked_ids:
            raise CasinoError("❌ لا يمكن إتمام التحدي إذا كان أحد الطرفين محظوراً من اللعب.")
        first = store.user(data, challenger)
        second = store.user(data, opponent)
        _afford(first, amount)
        _afford(second, amount)
        winner = random.choice((challenger, opponent))
        first["money"] += amount if winner == challenger else -amount
        second["money"] += amount if winner == opponent else -amount
    return winner