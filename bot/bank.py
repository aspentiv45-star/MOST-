"""Разбор уведомлений банка о поступлениях (Т-Банк и похожие).

Телефон с банковским приложением пересылает текст пуш-уведомления на сервер,
а бот ищет в нём суммы и сверяет их с уникальными суммами выставленных счетов.
"""
import re

# Сумма с копейками: «200,37 ₽», «+200.37 RUB», «1 200,05 р.» (пробелы — и неразрывные).
AMOUNT_RE = re.compile(
    r"(?P<sign>\+)?\s*(?P<rub>\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,](?P<kop>\d{1,2}))?"
    r"\s*(?P<cur>₽|руб\.?|rub|р\.)?",
    re.IGNORECASE,
)

INCOMING = ("пополнен", "поступ", "зачисл", "входящ", "перевод от", "получен", "+")
OUTGOING = ("покупка", "оплата", "списан", "снятие", "отказ", "вы перевели", "перевод на")


def is_incoming(text):
    low = text.lower()
    return any(w in low for w in INCOMING) and not any(w in low for w in OUTGOING)


def parse_amounts(text):
    """Все суммы из текста в копейках. Берём только числа со знаком ₽/RUB или с «+» впереди."""
    found = set()
    for m in AMOUNT_RE.finditer(text):
        if not (m.group("cur") or m.group("sign")):
            continue
        rub = int(re.sub(r"\D", "", m.group("rub")))
        kop = m.group("kop") or "0"
        found.add(rub * 100 + int(kop.ljust(2, "0")))
    return found


def fmt_amount(kopecks):
    return f"{kopecks // 100},{kopecks % 100:02d} ₽"
