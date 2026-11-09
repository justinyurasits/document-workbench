"""Amounts in words for the authorization paragraph: "five thousand six hundred fourteen dollars and 40/100"."""
from decimal import ROUND_HALF_UP, Decimal

ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
        "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
SCALES = [(10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")]


def below_thousand(number):
    words = []
    if number >= 100:
        words += [ONES[number // 100], "hundred"]
        number %= 100
    if number >= 20:
        words.append(TENS[number // 10] + (f"-{ONES[number % 10]}" if number % 10 else ""))
    elif number or not words:
        words.append(ONES[number])
    return " ".join(words)


def number_words(number):
    if number == 0:
        return "zero"
    words = []
    for scale, name in SCALES:
        if number >= scale:
            words.append(f"{below_thousand(number // scale)} {name}")
            number %= scale
    if number:
        words.append(below_thousand(number))
    return " ".join(words)


def dollars_in_words(amount):
    """Whole dollars in words and cents as a fraction, as on a check. The sign is stated by the caller."""
    amount = abs(Decimal(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    dollars, cents = divmod(int(amount * 100), 100)
    unit = "dollar" if dollars == 1 else "dollars"
    return f"{number_words(dollars)} {unit} and {cents:02d}/100"
