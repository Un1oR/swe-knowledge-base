from pathlib import Path


VERIFICATION_TAG = (
    '<meta name="google-site-verification" '
    'content="E0kYdRx7OkEM9IEXtNCMrOBQws41yD60oF3R_GPTYRg" />'
)


def add_site_verification(path: Path) -> None:
    html = path.read_text(encoding="utf-8")
    head, closing_tag, body = html.partition("</head>")
    if not closing_tag:
        raise ValueError(f"Missing </head> in {path}")
    if VERIFICATION_TAG not in head:
        path.write_text(head + VERIFICATION_TAG + closing_tag + body, encoding="utf-8")


if __name__ == "__main__":
    add_site_verification(Path(__file__).resolve().parents[1] / "_build/html/index.html")
