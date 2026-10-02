"""fetch_wikipedia_games.py のオフラインテスト。

    python3 -m unittest discover -s scripts
"""

import gzip
import json
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_wikipedia_games as fwg  # noqa: E402

LEAD_HTML = """
<div class="mw-content-ltr mw-parser-output" lang="ja" dir="ltr">
<table class="plainlinks ambox"><tbody><tr><th>注意</th><td>保守テンプレート</td></tr></tbody></table>
<table class="infobox bordered" style="width:300px">
<tbody>
<tr><th colspan="2">ポケットモンスター 赤・緑</th></tr>
<tr><td colspan="2"><span typeof="mw:File"><img src="x.png" alt=""></span></td></tr>
<tr><th>ジャンル</th><td><a href="/wiki/RPG" title="ロールプレイングゲーム">ロールプレイングゲーム</a></td></tr>
<tr><th>対応機種</th><td><a href="/wiki/GB">ゲームボーイ</a><style>.x{color:red}</style></td></tr>
<tr><th>開発元</th><td><a href="/wiki/GF">ゲームフリーク</a></td></tr>
<tr><th>発売日</th><td><span class="flagicon"><img alt="日本の旗" src="f.png"></span> 1996年2月27日<sup class="reference"><a href="#cite_note-1">[1]</a></sup><br>
<abbr>NA</abbr> 1998年9月28日</td></tr>
<tr><th>対象年齢</th><td><table class="nested"><tr><th>CERO</th><td>A</td></tr></table></td></tr>
<tr><th>売上本数</th><td>822万本 | 推定</td></tr>
</tbody>
</table>
<p>『<b>ポケットモンスター 赤・緑</b>』は、1996年に発売されたゲームソフト。</p>
<table class="wikitable"><tr><th>後続の表</th><td>拾ってはいけない</td></tr></table>
</div>
"""

EXTRACT = """『ポケットモンスター 赤・緑』は、1996年に発売されたゲームソフト。
# 行頭がシャープの行

== 概要 ==
ポケモンを捕まえて育てるRPG。
1. 番号付きに見える行

=== 空の節 ===

== 開発 ==

=== 経緯 ===
開発には6年かかった。

== 脚注 ==

=== 注釈 ===
削除されるべき注釈。

== 関連項目 ==
ポケットモンスター
"""


class FakeWikipedia(BaseHTTPRequestHandler):
    requests = []
    fail_first = 0
    maxlag_first = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        cls = type(self)
        params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        cls.requests.append({"params": params, "user_agent": self.headers.get("User-Agent")})

        if cls.fail_first:
            cls.fail_first -= 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        if cls.maxlag_first:
            cls.maxlag_first -= 1
            self._json({"error": {"code": "maxlag", "info": "Waiting for a database server"}}, {"Retry-After": "0"})
            return

        if params.get("action") == "query":
            title = params["titles"]
            if title == "ポケットモンスター ピカチュウ":
                page = {"ns": 0, "title": title, "missing": True}
            else:
                page = {
                    "pageid": 1,
                    "ns": 0,
                    "title": title,
                    "lastrevid": 98765,
                    "fullurl": "https://ja.wikipedia.org/wiki/" + title.replace(" ", "_"),
                    "extract": EXTRACT.split("\n\n==")[0] if params.get("exintro") else EXTRACT,
                }
            self._json({"batchcomplete": True, "query": {"pages": [page]}})
        elif params.get("action") == "parse":
            self._json({"parse": {"title": "x", "pageid": 1, "revid": int(params["oldid"]), "text": LEAD_HTML}})
        else:
            self.send_response(400)
            self.end_headers()

    def _json(self, data, headers=None):
        body = gzip.compress(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Encoding", "gzip")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWikipedia)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.api_url = f"http://127.0.0.1:{cls.server.server_address[1]}/w/api.php"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeWikipedia.requests = []
        FakeWikipedia.fail_first = 0
        FakeWikipedia.maxlag_first = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *extra):
        return fwg.main(["--out", str(self.out), "--delay", "0", "--api-url", self.api_url, *extra])

    def test_sends_policy_compliant_user_agent(self):
        self.assertEqual(self.run_main("--only", "gen1-red-green"), 0)
        user_agents = {r["user_agent"] for r in FakeWikipedia.requests}
        self.assertEqual(user_agents, {fwg.DEFAULT_USER_AGENT})
        self.assertIn("https://github.com/pokopoke/pokemon-card-gallery", fwg.DEFAULT_USER_AGENT)
        for r in FakeWikipedia.requests:
            self.assertEqual(r["params"]["maxlag"], "5")

    def test_custom_user_agent(self):
        self.assertEqual(self.run_main("--only", "gen1-red-green", "--user-agent", "MyBot/1.0 (me@example.com)"), 0)
        self.assertEqual({r["user_agent"] for r in FakeWikipedia.requests}, {"MyBot/1.0 (me@example.com)"})

    def test_retries_on_429_and_maxlag(self):
        FakeWikipedia.fail_first = 1
        FakeWikipedia.maxlag_first = 1
        self.assertEqual(self.run_main("--only", "gen1-red-green"), 0)
        self.assertTrue((self.out / "gen1-red-green.md").exists())

    def test_game_markdown(self):
        self.assertEqual(self.run_main("--only", "gen1-red-green"), 0)
        md = (self.out / "gen1-red-green.md").read_text(encoding="utf-8")

        self.assertTrue(md.startswith("# ポケットモンスター 赤・緑\n"))
        self.assertIn("CC BY-SA 4.0", md)
        self.assertIn("oldid=98765", md)
        self.assertIn("action=history", md)
        self.assertIn("\\# 行頭がシャープの行", md)
        self.assertIn("\\1. 番号付きに見える行", md)

        self.assertIn("| ジャンル | ロールプレイングゲーム |", md)
        self.assertIn("| 対応機種 | ゲームボーイ |", md)
        self.assertIn("| 発売日 | 1996年2月27日 / NA 1998年9月28日 |", md)
        self.assertIn("| 対象年齢 | CERO A |", md)
        self.assertIn("| 売上本数 | 822万本 \\| 推定 |", md)
        self.assertNotIn("保守テンプレート", md)
        self.assertNotIn("拾ってはいけない", md)
        self.assertNotIn("[1]", md)
        self.assertNotIn("color:red", md)

        self.assertIn("## 概要\n\nポケモンを捕まえて育てるRPG。\n", md)
        self.assertIn("## 開発\n\n### 経緯\n\n開発には6年かかった。\n", md)
        self.assertNotIn("空の節", md)
        self.assertNotIn("脚注", md)
        self.assertNotIn("注釈", md)
        self.assertNotIn("関連項目", md)

    def test_intro_only(self):
        self.assertEqual(self.run_main("--only", "gen1-red-green", "--intro-only"), 0)
        md = (self.out / "gen1-red-green.md").read_text(encoding="utf-8")
        self.assertEqual(FakeWikipedia.requests[0]["params"].get("exintro"), "1")
        self.assertNotIn("## 概要", md)
        self.assertIn("## 基本情報", md)

    def test_index_and_missing_pages(self):
        self.assertEqual(self.run_main("--only", "gen1-red-green", "--only", "gen1-pikachu"), 0)
        readme = (self.out / "README.md").read_text(encoding="utf-8")
        self.assertIn("| 第1世代 | [ポケットモンスター 赤・緑](gen1-red-green.md) | 1996年2月27日 | ゲームボーイ |", readme)
        self.assertIn("## 取得できなかった記事", readme)
        self.assertIn("ポケットモンスター ピカチュウ（`gen1-pikachu`）", readme)
        self.assertFalse((self.out / "gen1-pikachu.md").exists())

        index = json.loads((self.out / "index.json").read_text(encoding="utf-8"))
        self.assertEqual([g["slug"] for g in index["games"]], ["gen1-red-green"])
        self.assertEqual(index["games"][0]["revid"], 98765)
        self.assertEqual(index["games"][0]["infobox"]["開発元"], "ゲームフリーク")

    def test_partial_update_keeps_other_entries(self):
        self.assertEqual(self.run_main("--only", "gen2-gold-silver"), 0)
        self.assertEqual(self.run_main("--only", "gen1-red-green"), 0)
        index = json.loads((self.out / "index.json").read_text(encoding="utf-8"))
        self.assertEqual([g["slug"] for g in index["games"]], ["gen1-red-green", "gen2-gold-silver"])

    def test_forbidden_reports_user_agent_hint(self):
        client = fwg.WikipediaClient(self.api_url.replace("/w/api.php", "/forbidden"), "x", retries=0)
        with mock.patch.object(fwg, "urlopen", side_effect=fwg.HTTPError("u", 403, "Forbidden", {}, None)):
            with self.assertRaisesRegex(fwg.WikipediaError, "User-Agent"):
                client.get({"action": "query"})


class FilterSectionsTest(unittest.TestCase):
    def test_keeps_parent_of_non_empty_child_only(self):
        sections = [
            fwg.Section(2, "A", []),
            fwg.Section(3, "A-1", ["text"]),
            fwg.Section(2, "B", []),
            fwg.Section(3, "B-1", []),
            fwg.Section(2, "C", ["text"]),
        ]
        self.assertEqual([s.title for s in fwg.filter_sections(sections)], ["A", "A-1", "C"])


if __name__ == "__main__":
    unittest.main()
