#!/usr/bin/env python3
"""Wikipedia日本語版からポケモン本編ゲームの記事を取得し、Markdownにまとめる。

使い方:
    python3 scripts/fetch_wikipedia_games.py
    python3 scripts/fetch_wikipedia_games.py --only gen1-red-green --only gen9-scarlet-violet
    python3 scripts/fetch_wikipedia_games.py --intro-only

Wikimedia の User-Agent ポリシー (https://meta.wikimedia.org/wiki/User-Agent_policy) では
連絡先を含む User-Agent が必須で、汎用的なもの (Python-urllib など) は 403 で拒否される。
フォークして使う場合は --user-agent か環境変数 WIKIPEDIA_USER_AGENT で自分の連絡先に変えること。

標準ライブラリのみで動作する。
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_API_URL = "https://ja.wikipedia.org/w/api.php"
DEFAULT_USER_AGENT = (
    "PokemonCardGalleryBot/0.1 "
    "(https://github.com/pokopoke/pokemon-card-gallery) "
    f"Python-urllib/{sys.version_info.major}.{sys.version_info.minor}"
)
DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "wikipedia" / "games"

LICENSE_NAME = "クリエイティブ・コモンズ 表示-継承 4.0 国際 (CC BY-SA 4.0)"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/deed.ja"

# 本文として役に立たない節 (プレーンテキスト化すると中身が消える/リンク集になる)
SKIP_SECTIONS = {
    "脚注", "注釈", "出典", "脚注・出典", "注釈・出典", "参考文献", "参照",
    "関連項目", "外部リンク", "関連書籍",
}


@dataclass(frozen=True)
class Game:
    slug: str
    generation: int
    title: str


GAMES = [
    Game("gen1-red-green", 1, "ポケットモンスター 赤・緑"),
    Game("gen1-pikachu", 1, "ポケットモンスター ピカチュウ"),
    Game("gen2-gold-silver", 2, "ポケットモンスター 金・銀"),
    Game("gen2-crystal", 2, "ポケットモンスター クリスタルバージョン"),
    Game("gen3-ruby-sapphire", 3, "ポケットモンスター ルビー・サファイア"),
    Game("gen3-firered-leafgreen", 3, "ポケットモンスター ファイアレッド・リーフグリーン"),
    Game("gen3-emerald", 3, "ポケットモンスター エメラルド"),
    Game("gen4-diamond-pearl", 4, "ポケットモンスター ダイヤモンド・パール"),
    Game("gen4-platinum", 4, "ポケットモンスター プラチナ"),
    Game("gen4-heartgold-soulsilver", 4, "ポケットモンスター ハートゴールド・ソウルシルバー"),
    Game("gen5-black-white", 5, "ポケットモンスター ブラック・ホワイト"),
    Game("gen5-black2-white2", 5, "ポケットモンスター ブラック2・ホワイト2"),
    Game("gen6-x-y", 6, "ポケットモンスター X・Y"),
    Game("gen6-omegaruby-alphasapphire", 6, "ポケットモンスター オメガルビー・アルファサファイア"),
    Game("gen7-sun-moon", 7, "ポケットモンスター サン・ムーン"),
    Game("gen7-ultrasun-ultramoon", 7, "ポケットモンスター ウルトラサン・ウルトラムーン"),
    Game("gen7-lets-go-pikachu-eevee", 7, "ポケットモンスター Let's Go! ピカチュウ・Let's Go! イーブイ"),
    Game("gen8-sword-shield", 8, "ポケットモンスター ソード・シールド"),
    Game("gen8-brilliantdiamond-shiningpearl", 8, "ポケットモンスター ブリリアントダイヤモンド・シャイニングパール"),
    Game("gen8-legends-arceus", 8, "Pokémon LEGENDS アルセウス"),
    Game("gen9-scarlet-violet", 9, "ポケットモンスター スカーレット・バイオレット"),
    Game("gen9-legends-z-a", 9, "Pokémon LEGENDS Z-A"),
]

# 一覧表に出す項目と、インフォボックス上で取りうるラベル
INDEX_FIELDS = {
    "発売日": ("発売日",),
    "対応機種": ("対応機種", "機種"),
}


class WikipediaError(Exception):
    pass


class WikipediaClient:
    def __init__(self, api_url: str, user_agent: str, retries: int = 4, backoff: float = 2.0):
        self.api_url = api_url
        self.user_agent = user_agent
        self.retries = retries
        self.backoff = backoff

    def get(self, params: dict) -> dict:
        query = {"format": "json", "formatversion": "2", "maxlag": "5", **params}
        url = f"{self.api_url}?{urlencode(query)}"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        for attempt in range(self.retries + 1):
            last = attempt == self.retries
            try:
                with urlopen(Request(url, headers=headers), timeout=30) as res:
                    body = res.read()
                    if res.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    retry_after = res.headers.get("Retry-After")
                data = json.loads(body)
            except HTTPError as e:
                if e.code == 403:
                    raise WikipediaError(
                        f"403 Forbidden: {url}\n"
                        "User-Agent が Wikimedia のポリシーを満たしていない可能性があります。"
                        "--user-agent で連絡先 (URL かメールアドレス) を含む値を指定してください。"
                    ) from e
                if e.code in (429, 500, 502, 503, 504) and not last:
                    self._wait(attempt, e.headers.get("Retry-After"))
                    continue
                raise WikipediaError(f"HTTP {e.code}: {url}") from e
            except URLError as e:
                if "Tunnel connection failed" in str(e.reason):
                    raise WikipediaError(
                        f"プロキシが {self.api_url} への接続を拒否しました ({e.reason})。"
                        "ネットワーク設定で ja.wikipedia.org へのアクセスを許可してください。"
                    ) from e
                if not last:
                    self._wait(attempt, None)
                    continue
                raise WikipediaError(f"接続に失敗しました: {e.reason}") from e

            error = data.get("error")
            if error:
                if error.get("code") == "maxlag" and not last:
                    self._wait(attempt, retry_after)
                    continue
                raise WikipediaError(f"API エラー {error.get('code')}: {error.get('info')}")
            return data
        raise AssertionError("unreachable")

    def _wait(self, attempt: int, retry_after: str | None) -> None:
        try:
            seconds = float(retry_after) if retry_after else self.backoff * 2**attempt
        except ValueError:
            seconds = self.backoff * 2**attempt
        time.sleep(seconds)

    def fetch_page(self, title: str, intro_only: bool = False) -> dict | None:
        params = {
            "action": "query",
            "prop": "extracts|info",
            "titles": title,
            "redirects": "1",
            "explaintext": "1",
            "exsectionformat": "wiki",
            "inprop": "url",
        }
        if intro_only:
            params["exintro"] = "1"
        page = self.get(params)["query"]["pages"][0]
        if page.get("missing") or page.get("invalid"):
            return None
        return page

    def fetch_lead_html(self, revid: int) -> str:
        data = self.get({
            "action": "parse",
            "oldid": str(revid),
            "prop": "text",
            "section": "0",
            "disablelimitreport": "1",
            "disableeditsection": "1",
        })
        return data["parse"]["text"]


class InfoboxParser(HTMLParser):
    """最初の table.infobox から (見出し, 値) の行を取り出す。"""

    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, str]] = []
        self._inside = False
        self._done = False
        self._stack: list[tuple[str, bool]] = []
        self._row: list[tuple[str, str]] | None = None
        self._cell: tuple[str, list[str]] | None = None

    def _skipping(self) -> bool:
        return any(skip for _, skip in self._stack)

    def _nested_table(self) -> bool:
        return any(tag == "table" for tag, _ in self._stack)

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if not self._inside:
            if tag == "table" and "infobox" in classes:
                self._inside = True
            return
        if tag in self.VOID_TAGS:
            if tag == "br":
                self._add_text("\n")
            return
        style = (attrs.get("style") or "").replace(" ", "").lower()
        skip = (
            tag in ("style", "script")
            or "reference" in classes
            or "noprint" in classes
            or "display:none" in style
        )
        nested = self._nested_table()
        self._stack.append((tag, skip))
        if nested or self._skipping():
            if tag in ("li", "tr", "p", "div"):
                self._add_text("\n")
            elif tag in ("th", "td"):
                self._add_text(" ")
            return
        if tag == "tr":
            self._row = []
        elif tag in ("th", "td") and self._row is not None:
            self._cell = (tag, [])
        elif tag in ("li", "p", "div"):
            self._add_text("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self._done or not self._inside or tag in self.VOID_TAGS:
            return
        if tag == "table" and not self._nested_table():
            self._done = True
            return
        if not any(t == tag for t, _ in self._stack):
            return
        while self._stack:
            popped, _ = self._stack.pop()
            if popped == tag:
                break
        if self._nested_table():
            return
        if tag in ("th", "td") and self._cell is not None and self._row is not None:
            kind, parts = self._cell
            self._row.append((kind, clean_text("".join(parts))))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            kinds = [kind for kind, _ in self._row]
            if kinds in (["th", "td"], ["td", "td"]) and self._row[0][1] and self._row[1][1]:
                self.rows.append((self._row[0][1].rstrip(":："), self._row[1][1]))
            self._row = None

    def handle_data(self, data):
        if self._inside and not self._done:
            self._add_text(data)

    def _add_text(self, text: str) -> None:
        if self._cell is not None and not self._skipping():
            self._cell[1].append(text)


def clean_text(text: str) -> str:
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.split("\n"))
    return " / ".join(line for line in lines if line)


def parse_infobox(html: str) -> list[tuple[str, str]]:
    parser = InfoboxParser()
    parser.feed(html)
    parser.close()
    return parser.rows


@dataclass
class Section:
    level: int
    title: str
    paragraphs: list[str]


HEADING_RE = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$")
MARKDOWN_LEADING_RE = re.compile(r"^([#>*+\-|=`]|\d+[.)]\s)")


def escape_line(line: str) -> str:
    if MARKDOWN_LEADING_RE.match(line):
        return "\\" + line
    return line


def split_sections(extract: str) -> tuple[list[str], list[Section]]:
    lead: list[str] = []
    sections: list[Section] = []
    for raw in extract.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = HEADING_RE.match(line)
        if m:
            sections.append(Section(len(m.group(1)), m.group(2), []))
        elif sections:
            sections[-1].paragraphs.append(line)
        else:
            lead.append(line)
    return lead, sections


def filter_sections(sections: list[Section]) -> list[Section]:
    kept: list[Section] = []
    skip_level = None
    for section in sections:
        if skip_level is not None and section.level > skip_level:
            continue
        skip_level = None
        if section.title in SKIP_SECTIONS:
            skip_level = section.level
            continue
        kept.append(section)

    # 本文も、中身のある子節も持たない見出しを落とす
    result: list[Section] = []
    has_content_below: dict[int, bool] = {}
    for section in reversed(kept):
        child = any(v for lvl, v in has_content_below.items() if lvl > section.level)
        keep = bool(section.paragraphs) or child
        for lvl in [lvl for lvl in has_content_below if lvl > section.level]:
            del has_content_below[lvl]
        has_content_below[section.level] = has_content_below.get(section.level, False) or keep
        if keep:
            result.append(section)
    result.reverse()
    return result


def md_cell(text: str) -> str:
    return text.replace("|", "\\|")


def page_url(api_url: str, title: str) -> str:
    base = api_url.rsplit("/w/api.php", 1)[0]
    return f"{base}/wiki/{quote(title.replace(' ', '_'))}"


def permalink(api_url: str, title: str, revid: int) -> str:
    base = api_url.rsplit("/api.php", 1)[0]
    return f"{base}/index.php?{urlencode({'title': title, 'oldid': revid})}"


def history_url(api_url: str, title: str) -> str:
    base = api_url.rsplit("/api.php", 1)[0]
    return f"{base}/index.php?{urlencode({'title': title, 'action': 'history'})}"


def pick_field(infobox: list[tuple[str, str]], labels: tuple[str, ...]) -> str:
    for label, value in infobox:
        if label in labels:
            return value
    return ""


def render_game(game: Game, page: dict, infobox: list[tuple[str, str]], api_url: str, retrieved: str) -> str:
    title = page["title"]
    revid = page["lastrevid"]
    url = page.get("fullurl") or page_url(api_url, title)
    lead, sections = split_sections(page.get("extract", ""))
    sections = filter_sections(sections)

    out = [f"# {title}", ""]
    out.append(
        f"> **出典**: Wikipedia日本語版「[{title}]({url})」"
        f"（[版 {revid}]({permalink(api_url, title, revid)})、{retrieved} 取得）  "
    )
    out.append(
        f"> 本文は [{LICENSE_NAME}]({LICENSE_URL}) の下で利用しています。"
        f"執筆者は[版の履歴]({history_url(api_url, title)})を参照してください。"
    )
    out.append("")
    out.append(f"- 世代: 第{game.generation}世代")
    out.append("")

    for line in lead:
        out += [escape_line(line), ""]

    if infobox:
        out += ["## 基本情報", "", "| 項目 | 内容 |", "| --- | --- |"]
        out += [f"| {md_cell(label)} | {md_cell(value)} |" for label, value in infobox]
        out.append("")

    for section in sections:
        out += [f"{'#' * section.level} {section.title}", ""]
        for line in section.paragraphs:
            out += [escape_line(line), ""]

    return "\n".join(out).rstrip() + "\n"


def render_index(entries: list[dict], missing: list[Game], retrieved: str) -> str:
    out = [
        "# ポケモン本編ゲーム一覧（Wikipedia日本語版より）",
        "",
        "このディレクトリは `scripts/fetch_wikipedia_games.py` で生成しています。"
        "手で編集せず、スクリプトを再実行して更新してください。",
        "",
        f"取得日: {retrieved}",
        "",
        "| 世代 | タイトル | 発売日 | 対応機種 |",
        "| --- | --- | --- | --- |",
    ]
    for entry in entries:
        fields = entry["fields"]
        release = fields["発売日"].split(" / ")[0]
        out.append(
            f"| 第{entry['generation']}世代 | [{md_cell(entry['title'])}]({entry['file']}) "
            f"| {md_cell(release)} | {md_cell(fields['対応機種'])} |"
        )
    out.append("")
    if missing:
        out += ["## 取得できなかった記事", ""]
        out += [f"- {game.title}（`{game.slug}`）" for game in missing]
        out.append("")
    out += [
        "## ライセンス",
        "",
        f"各ファイルの本文は Wikipedia日本語版の記事に由来し、[{LICENSE_NAME}]({LICENSE_URL}) の下で利用しています。"
        "再配布・改変する場合も同じライセンスを適用し、各ファイル冒頭の出典表記を残してください。",
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR, help="出力先ディレクトリ")
    parser.add_argument("--only", action="append", metavar="SLUG", help="指定したスラッグの作品だけ取得する (複数指定可)")
    parser.add_argument("--intro-only", action="store_true", help="本文は記事冒頭の導入部だけにする")
    parser.add_argument("--delay", type=float, default=1.0, help="リクエスト間の待ち時間 (秒)")
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("WIKIPEDIA_USER_AGENT", DEFAULT_USER_AGENT),
        help="送信する User-Agent (既定: 環境変数 WIKIPEDIA_USER_AGENT か組み込み値)",
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    games = GAMES
    if args.only:
        known = {game.slug for game in GAMES}
        unknown = [slug for slug in args.only if slug not in known]
        if unknown:
            parser.error(f"不明なスラッグ: {', '.join(unknown)}")
        games = [game for game in GAMES if game.slug in args.only]

    client = WikipediaClient(args.api_url, args.user_agent)
    retrieved = datetime.date.today().isoformat()
    args.out.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    missing: list[Game] = []
    for i, game in enumerate(games):
        if i:
            time.sleep(args.delay)
        print(f"[{i + 1}/{len(games)}] {game.title}", file=sys.stderr)
        try:
            page = client.fetch_page(game.title, intro_only=args.intro_only)
            if page is None:
                print(f"  記事が見つかりません: {game.title}", file=sys.stderr)
                missing.append(game)
                continue
            time.sleep(args.delay)
            infobox = parse_infobox(client.fetch_lead_html(page["lastrevid"]))
        except WikipediaError as e:
            print(f"エラー: {e}", file=sys.stderr)
            return 1

        filename = f"{game.slug}.md"
        (args.out / filename).write_text(
            render_game(game, page, infobox, args.api_url, retrieved), encoding="utf-8"
        )
        entries.append({
            "slug": game.slug,
            "generation": game.generation,
            "title": page["title"],
            "file": filename,
            "url": page.get("fullurl") or page_url(args.api_url, page["title"]),
            "revid": page["lastrevid"],
            "fields": {key: pick_field(infobox, labels) for key, labels in INDEX_FIELDS.items()},
            "infobox": dict(infobox),
        })

    # --only で一部だけ取得した場合も、既存の一覧とマージして全体を保つ
    index_json = args.out / "index.json"
    if args.only and index_json.exists():
        previous = json.loads(index_json.read_text(encoding="utf-8"))["games"]
        fetched = {entry["slug"] for entry in entries}
        entries += [entry for entry in previous if entry["slug"] not in fetched]
        order = {game.slug: n for n, game in enumerate(GAMES)}
        entries.sort(key=lambda entry: order.get(entry["slug"], len(order)))

    index_json.write_text(
        json.dumps({"retrieved": retrieved, "source": args.api_url, "games": entries}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (args.out / "README.md").write_text(render_index(entries, missing, retrieved), encoding="utf-8")
    print(f"{len(entries)} 件を {args.out} に書き出しました。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
