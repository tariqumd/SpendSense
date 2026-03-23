import re

DEFAULT_CATEGORY_KEYWORDS = {
    "emi": [
        "emi",
        "emis",
        "loans",
        "loan payment"
    ],
    "food": [
        "food",
        "swiggy",
        "zomato",
        "outside food",
        "outside lunch",
        "outside dinner",
        "lunch",
        "dinner",
        "breakfast",
        "brunch",
        "tiffin",
        "snack",
        "snacks",
        "momos",
        "juice",
        "office meal",
        "restaurant",
        "cafe",
        "hotel",
        "biryani",
    ],
    "groceries": [
        "groceries",
        "grocs",
        "zepto",
        "chicken",
        "fruits",
        "veggies",
        "eggs",
    ],
    "transport": [
        "petrol",
        "fuel",
        "diesel",
        "rapido",
        "auto",
        "parking",
        "uber",
        "ola",
        "bike repair",
        "car repair",
        "bike service",
        "car service",
        "bike",
        "car",
    ],
    "home": [
        "rent",
        "maintenance",
        "water bill",
        "eb bill",
        "electricity",
        "gas booking",
        "home repair",
        "home things"
    ],
    "bills": [
        "insurance",
        "card bill",
        "credit card",
        "bill",
        "wifi",
        "internet",
        "recharge",
    ],
    "subscriptions": [
        "netflix",
        "subscription",
        "autopay",
    ],
    "shopping": [
        "amazon",
        "flipkart",
        "shopping",
        "myntra",
        "dress",
        "personal care",
    ],
    "travel": [
        "trip",
        "travel",
    ],
    "health": [
        "hair spa",
        "massage",
        "medical",
        "clinic",
        "pharmacy",
    ],
    "misc": [
        "misc",
    ],
    "Entertainment": [
        "movie",
        "concert",
        "bookmyshow",
    ],
}

CREDIT_CATEGORY_KEYWORDS = {
    "salary": ["salary", "payroll", "payout"],
    "refund": ["refund", "reversal", "cashback", "returned"],
    "borrowed": ["borrowed", "loan received", "lend", "borrow", "received from"],
    "interest": ["interest"],
    "other_credit": ["credit", "deposit", "received"],
}

AMOUNT_PATTERN = re.compile(r"(\d+(?:\.\d{1,2})?)")

CATEGORY_PRIORITY = {
    "emi": 110,
    "bills": 100,
    "subscriptions": 90,
    "home": 80,
    "transport": 70,
    "groceries": 60,
    "food": 50,
    "shopping": 40,
    "travel": 30,
    "health": 20,
    "misc": 10,
}


def normalize_rule_name(name):
    normalized = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return normalized or "other"


def normalize_keywords(raw_keywords):
    return [keyword.strip().lower() for keyword in raw_keywords if keyword and keyword.strip()]


def merge_category_keywords(extra_rules=None):
    merged = {}

    for name, keywords in (extra_rules or {}).items():
        normalized_name = normalize_rule_name(name)
        merged[normalized_name] = normalize_keywords(keywords)

    for name, keywords in DEFAULT_CATEGORY_KEYWORDS.items():
        normalized_name = normalize_rule_name(name)
        merged.setdefault(normalized_name, [])
        existing = set(merged[normalized_name])
        for keyword in normalize_keywords(keywords):
            if keyword not in existing:
                merged[normalized_name].append(keyword)
                existing.add(keyword)

    return merged


def parse_expense_input(text, extra_rules=None, transaction_type="debit"):
    raw_text = (text or "").strip()
    if not raw_text:
        raise ValueError("Please enter an expense.")

    normalized_text = raw_text.lower()
    amount_match = AMOUNT_PATTERN.search(normalized_text)

    if not amount_match:
        raise ValueError("Couldn't find an amount. Try something like '250 on fuel'.")

    amount = float(amount_match.group(1))
    category = "other_credit" if transaction_type == "credit" else "other"

    category_rules = (
        CREDIT_CATEGORY_KEYWORDS
        if transaction_type == "credit"
        else merge_category_keywords(extra_rules)
    )

    best_match = None

    for name, keywords in category_rules.items():
        for keyword in keywords:
            if keyword in normalized_text:
                current_match = (
                    CATEGORY_PRIORITY.get(name, 0),
                    len(keyword),
                    name,
                )
                if best_match is None or current_match > best_match:
                    best_match = current_match
                    category = name

    if best_match is None:
        category = "other_credit" if transaction_type == "credit" else "other"

    return {
        "amount": amount,
        "category": category,
        "note": raw_text,
    }
