"""
Cheap pre-filter: checks yt-dlp metadata (title, description, uploader)
for food/restaurant keywords to avoid sending non-food videos to Gemini.
"""

import re

# High-confidence food words (score +3 each)
HIGH_KEYWORDS = [
    'restaurant', 'cafe', 'café', 'bistro', 'omakase', 'ramen', 'sushi',
    'pizzeria', 'diner', 'eatery', 'foodtok', 'foodie', 'mukbang',
    'michelin', 'foodcrawl', 'food crawl', 'food tour', 'food review',
    'food spot', 'food vlog', 'food blog', 'must eat', 'must try',
    'best eats', 'where to eat', 'eating tour', 'street food',
    'food guide', 'food find', 'food hack', 'food rec',
    'izakaya', 'trattoria', 'gastropub', 'hawker',
    # Indonesian food keywords
    'resto', 'warung', 'kuliner', 'kulineran', 'makan', 'makanan',
    'kopitiam', 'bakso', 'prasmanan', 'foodmap', 'jktfood', 'jktgo',
    # Japanese food keywords
    'yakitori', 'yakibuta', 'tempura', 'udon', 'donburi',
]

# Medium-confidence food words (score +1 each)
MEDIUM_KEYWORDS = [
    'food', 'eat', 'eating', 'ate', 'brunch', 'lunch', 'dinner',
    'breakfast', 'cook', 'cooking', 'recipe', 'dish', 'menu', 'taste',
    'tasting', 'delicious', 'yummy', 'hungry', 'bao', 'pho', 'taco',
    'burger', 'steak', 'pasta', 'noodle', 'noodles', 'bbq', 'barbecue',
    'seafood', 'dessert', 'bakery', 'hidden gem', 'chef', 'kitchen',
    'appetizer', 'entree', 'cocktail', 'wine bar', 'bar food',
    'dim sum', 'dumpling', 'pizza', 'curry', 'thai', 'korean',
    'japanese', 'mexican', 'italian', 'chinese', 'vietnamese',
    'indian', 'mediterranean', 'greek', 'french cuisine',
    'spicy', 'crispy', 'grilled', 'fried', 'roasted',
    # Indonesian food words
    'nasi', 'mie', 'bakmi', 'bakmie', 'babi', 'ayam', 'bebek',
    'sambal', 'goreng', 'soto', 'enak', 'cobain', 'nyobain',
    'batagor', 'cuankie', 'misoa', 'hainam', 'tiramisu',
    'lauknya', 'pedes', 'viral', 'hits',
    # Japanese food words
    'tonkotsu', 'matcha', 'gyoza', 'katsu', 'bento', 'onigiri',
]

# Anti-keywords — strong signals this is NOT a restaurant video (score -5 each)
ANTI_KEYWORDS = [
    'tutorial', 'gaming', 'makeup', 'dance challenge', 'fitness',
    'workout', 'news', 'politics', 'unboxing tech', 'coding',
    'programming', 'skincare', 'fashion haul', 'prank',
    'diy craft', 'home decor diy',
]


def is_likely_restaurant(metadata: dict) -> str:
    """
    Score metadata for restaurant likelihood.

    Args:
        metadata: dict with keys like 'title', 'description', 'uploader', 'tags'
                  (as returned by yt-dlp extract_info)

    Returns:
        "likely" (score >= 3), "maybe" (score >= -1), or "unlikely" (score < -1)
    """
    # Build text blob from all available metadata
    title = (metadata.get('title') or '').lower()
    description = (metadata.get('description') or '').lower()
    uploader = (metadata.get('uploader') or '').lower()
    tags = ' '.join(metadata.get('tags') or []).lower()

    text = f'{title} {description} {uploader} {tags}'

    score = 0

    for kw in HIGH_KEYWORDS:
        if kw.lower() in text:
            score += 3

    for kw in MEDIUM_KEYWORDS:
        if kw.lower() in text:
            score += 1

    for kw in ANTI_KEYWORDS:
        if kw.lower() in text:
            score -= 2

    if score >= 3:
        return 'likely'
    elif score >= -1:
        return 'maybe'
    else:
        return 'unlikely'


if __name__ == '__main__':
    # Quick test with a sample metadata dict
    import json
    import sys

    if len(sys.argv) < 2:
        print('Usage: python3 metadata_filter.py <info.json path>')
        sys.exit(1)

    with open(sys.argv[1]) as f:
        meta = json.load(f)

    result = is_likely_restaurant(meta)
    print(f'Title: {meta.get("title", "")[:80]}')
    print(f'Uploader: {meta.get("uploader", "")}')
    print(f'Result: {result}')
