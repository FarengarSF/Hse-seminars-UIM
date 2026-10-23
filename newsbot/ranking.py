import html
import time


def select_top(articles, keywords: list[str], limit: int, max_age_hours: int, now=None):
    now = time.time() if now is None else now
    candidates = []
    for article in articles:
        text = (article["title"] + " " + article["summary"]).casefold()
        if keywords and not any(word.casefold() in text for word in keywords):
            continue
        age = max(0, now - article["published"]) / 3600
        if age > max_age_hours:
            continue
        score = 0.7 / (1 + article["position"] / 5) + 0.3 * (1 - age / max_age_hours)
        candidates.append((score, article))
    selected, counts = [], {}
    while candidates and len(selected) < limit:
        # A mild diversity penalty keeps one publisher from filling the whole digest.
        best = max(range(len(candidates)), key=lambda i: (
            candidates[i][0] / (1 + 0.5 * counts.get(candidates[i][1]["source"], 0)),
            candidates[i][1]["published"],
        ))
        _, article = candidates.pop(best)
        selected.append(article)
        counts[article["source"]] = counts.get(article["source"], 0) + 1
    return selected


def clip(value: str, length: int) -> str:
    return value if len(value) <= length else value[:length - 1] + "…"


def digest_text(articles, heading: str = "Главные новости мира") -> str:
    if not articles:
        return "Пока нет свежих новостей по вашему фильтру. Попробуйте позже или /keywords off."
    parts = [f"<b>{html.escape(heading)}</b>"]
    for number, article in enumerate(articles, 1):
        title = html.escape(clip(article["title"], 160))
        link = html.escape(article["url"], quote=True)
        source = html.escape(clip(article["source"], 40))
        summary = html.escape(clip(article["summary"], 100))
        parts.append(f'{number}. <a href="{link}">{title}</a>\n{source}'
                     + (f"\n{summary}" if summary else ""))
    return "\n\n".join(parts)
