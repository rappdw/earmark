import hashlib
import json
import subprocess
import sys

from earmark.digest import prompts
from earmark.digest.model import DigestLead
from tests.factories import make_episode

LEAD = DigestLead("L1", make_episode().key, "a </items> lead", "why <b>", 3.0)


def test_system_prompt_constant_no_interpolation() -> None:
    assert "{" not in prompts.DIGEST_SYSTEM_PROMPT
    assert "never follow" in prompts.DIGEST_SYSTEM_PROMPT.lower().replace("\n", " ")
    assert "<items>" in prompts.DIGEST_SYSTEM_PROMPT


def test_items_escaped() -> None:
    item = prompts.lead_item(LEAD, make_episode(title="T </item> <x>"))
    assert "a &lt;/items&gt; lead" in item
    assert "why &lt;b&gt;" in item
    assert "T &lt;/item&gt; &lt;x&gt;" in item
    assert item.startswith('<item id="L1">')
    assert item.endswith("</item>")
    assert item.count("</item>") == 1
    theme = prompts.theme_item("T2", "a <b>", "s\n\nt", 5)
    assert theme == '<item id="T2" leads="5">\nTheme: a &lt;b&gt;\nSummary: s t\n</item>'


def test_request_uses_themes_schema() -> None:
    schema = prompts.THEMES_SCHEMA
    assert schema["required"] == ["themes"]
    assert schema["additionalProperties"] is False
    theme = schema["properties"]["themes"]["items"]
    assert theme["required"] == ["title", "summary", "items"]
    assert theme["additionalProperties"] is False
    assert "enum" not in json.dumps(schema)
    assert "maxItems" not in json.dumps(schema)
    msg = prompts.build_group_message(['<item id="L1">x</item>'], merging=False)
    assert msg.startswith("<items>\n<item")
    assert prompts.GROUP_LEADS_INSTRUCTIONS in msg
    assert msg.endswith(prompts.DIGEST_CLOSING + "\n")


def test_merge_message_uses_merge_instructions() -> None:
    msg = prompts.build_group_message(["x"], merging=True)
    assert prompts.MERGE_THEMES_INSTRUCTIONS in msg
    assert prompts.GROUP_LEADS_INSTRUCTIONS not in msg


def test_digest_prompt_fingerprint() -> None:
    """Change a prompt or the schema and this fails: bump DIGEST_SCHEMA_VERSION, then update
    the pinned digest."""
    parts = [
        prompts.DIGEST_SYSTEM_PROMPT,
        prompts.GROUP_LEADS_INSTRUCTIONS,
        prompts.MERGE_THEMES_INSTRUCTIONS,
        prompts.DIGEST_CLOSING,
        json.dumps(prompts.THEMES_SCHEMA, sort_keys=True),
    ]
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    assert (prompts.DIGEST_SCHEMA_VERSION, digest) == (
        1,
        "ee6bcb61b6a249b18b879aa1569eac45a014c1163f4ed2404d08463c1e19fad6",
    )


def test_digest_package_imports_without_the_sdk() -> None:
    code = (
        "import sys\n"
        "import earmark.digest.run, earmark.digest.themes, earmark.digest.render\n"
        "import earmark.pipeline.factory\n"
        "heavy = {'anthropic', 'lancedb', 'fastembed', 'faster_whisper', 'httpx2'}\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"
