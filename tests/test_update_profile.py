from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import update_profile  # noqa: E402


NOW = dt.datetime(2026, 8, 29, 12, 0, tzinfo=dt.timezone.utc)


def payload(*, synced_at: str = "2026-08-29T06:02:02Z") -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": "2026-08-29T06:05:00Z",
        "htb": {
            "synced_at": synced_at,
            "name": "foobarto",
            "rank": "Elite Hacker",
            "ranking": 284,
            "user_owns": 501,
            "system_owns": 497,
            "xp_level": 124,
            "xp_level_title": "Grandmaster",
            "xp_level_grade": 3,
        },
        "signals": [
            {
                "kind": "Writing",
                "title": "Ignorance Is a Security Boundary",
                "date": "2026-08-28",
                "url": "https://foobarto.me/blog/2026/ignorance-is-a-security-boundary/",
            },
            {
                "kind": "Research",
                "title": "Fluency as Attack Surface",
                "date": "2026-05-26",
                "url": "https://doi.org/10.5281/zenodo.20397965",
            },
            {
                "kind": "Disclosure",
                "title": "Remote-controlled VS Code command execution",
                "date": "2026-08-18",
                "url": "https://foobarto.me/disclosures/vscode-antigravity-cockpit-command-execution/",
            },
            {
                "kind": "HTB",
                "title": "Atlas — HTB machine writeup",
                "date": "2026-05-17",
                "url": "https://foobarto.me/htb/machines/atlas/",
            },
        ],
    }


README = '''<!-- profile-console:start -->
placeholder
<!-- profile-console:end -->

<!-- profile-signals:start -->
placeholder
<!-- profile-signals:end -->
'''


class UpdateProfileTests(unittest.TestCase):
    def test_payload_rejects_stale_snapshot(self) -> None:
        data = payload(synced_at="2026-08-20T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "stale"):
            update_profile.parse_payload(json.dumps(data), now=NOW)

    def test_payload_rejects_off_domain_link(self) -> None:
        data = payload()
        data["signals"][0]["url"] = "https://example.com/trap"
        with self.assertRaisesRegex(ValueError, "allowlist"):
            update_profile.parse_payload(json.dumps(data), now=NOW)

    def test_payload_rejects_markdown_link_injection(self) -> None:
        data = payload()
        data["signals"][0]["url"] = "https://foobarto.me/ok) [trap](https://example.com"
        with self.assertRaisesRegex(ValueError, "unsafe characters"):
            update_profile.parse_payload(json.dumps(data), now=NOW)

    def test_payload_rejects_wrong_profile_identity(self) -> None:
        data = payload()
        data["htb"]["name"] = "someone-else"
        with self.assertRaisesRegex(ValueError, "identity"):
            update_profile.parse_payload(json.dumps(data), now=NOW)

    def test_payload_rejects_duplicate_signal_kind(self) -> None:
        data = payload()
        data["signals"][2]["kind"] = "Writing"
        with self.assertRaisesRegex(ValueError, "duplicate"):
            update_profile.parse_payload(json.dumps(data), now=NOW)

    def test_payload_requires_all_four_signal_kinds(self) -> None:
        data = payload()
        data["signals"].pop()
        with self.assertRaisesRegex(ValueError, "exactly four"):
            update_profile.parse_payload(json.dumps(data), now=NOW)

    def test_markdown_metacharacters_are_treated_as_text(self) -> None:
        data = payload()
        data["signals"][0]["title"] = (
            r"Boundary [notes] \1 <!-- profile-signals:end --> *literal*"
        )
        _, signals = update_profile.parse_payload(json.dumps(data), now=NOW)

        rendered = update_profile.replace_block(
            README,
            update_profile.SIGNALS_START,
            update_profile.SIGNALS_END,
            update_profile.signals_block(signals),
        )

        self.assertIn(
            r"Boundary \[notes\] \\1 &lt;!-- profile-signals:end --&gt; "
            r"\*literal\*",
            rendered,
        )
        self.assertEqual(rendered.count(update_profile.SIGNALS_END), 1)

    def test_svg_is_well_formed_and_contains_no_active_content(self) -> None:
        profile, _ = update_profile.parse_payload(json.dumps(payload()), now=NOW)
        for theme in ("dark", "light"):
            svg = update_profile.render_svg(profile, theme=theme)
            root = ET.fromstring(svg)
            self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
            for element in root.iter():
                self.assertNotEqual(element.tag.rsplit("}", 1)[-1].lower(), "script")
                self.assertNotEqual(
                    element.tag.rsplit("}", 1)[-1].lower(), "foreignobject"
                )
                for attribute, value in element.attrib.items():
                    self.assertFalse(attribute.lower().startswith("on"))
                    if attribute.rsplit("}", 1)[-1] == "href":
                        self.assertTrue(value.startswith("#"))
            self.assertIn("ELITE HACKER", svg)
            self.assertIn("USER 501", svg)

    def test_generation_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.joinpath("README.md").write_text(README, encoding="utf-8")
            source = root / "profile-signals.json"
            source.write_text(json.dumps(payload()), encoding="utf-8")

            changed = update_profile.generate(root, str(source), now=NOW)
            self.assertEqual(len(changed), 3)
            self.assertEqual(update_profile.generate(root, str(source), now=NOW), [])
            readme = root.joinpath("README.md").read_text(encoding="utf-8")
            self.assertIn("Ignorance Is a Security Boundary", readme)
            self.assertIn("Fluency as Attack Surface", readme)
            self.assertEqual(
                readme.count(
                    "curl -fsS https://foobarto.me/profile-signals.json "
                    "| jq -c '.signals|sort_by(.date)|reverse[]'"
                ),
                1,
            )
            self.assertEqual(readme.count("\n- `2026-"), 4)
            self.assertNotIn("Browse:", readme)
            self.assertNotIn('<p align="center">', readme)
            self.assertIn(
                "https://foobarto.me/htb/machines/atlas/", readme
            )
            self.assertLess(
                readme.index("Ignorance Is a Security Boundary"),
                readme.index("Remote-controlled VS Code command execution"),
            )
            self.assertLess(
                readme.index("Remote-controlled VS Code command execution"),
                readme.index("Fluency as Attack Surface"),
            )
            self.assertLess(
                readme.index("Fluency as Attack Surface"),
                readme.index("Atlas — HTB machine writeup"),
            )
            self.assertNotIn("placeholder", readme)


if __name__ == "__main__":
    unittest.main()
