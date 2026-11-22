import tomllib

import pytest

from phonectl import setup as st

URL, TOKEN = "http://127.0.0.1:47321/mcp", "tok"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "STATE", tmp_path / "setup.json")
    monkeypatch.setattr(st, "SKILL_BACKUPS", tmp_path / "skill-backups")
    monkeypatch.setattr(st, "CODEX_TOML", tmp_path / "codex" / "config.toml")
    monkeypatch.setattr(st, "DSH_PATCH", tmp_path / "dsh" / "cordis.patch.yml")
    return tmp_path


def test_codex_gets_one_phone_table_and_keeps_the_rest(home):
    st.CODEX_TOML.parent.mkdir()
    st.CODEX_TOML.write_text('model = "x"\n\n[mcp_servers.phone]\nurl = "old"\n\n[mcp_servers.phone.env]\nA = "1"\n\n'
                             '[mcp_servers.other]\ncommand = "y"\n')
    changed = []
    st._codex_add(URL, TOKEN, changed)
    st._codex_add(URL, TOKEN, changed)  # again: still one table
    conf = tomllib.loads(st.CODEX_TOML.read_text())
    assert conf["mcp_servers"]["phone"] == {"url": URL, "http_headers": {"Authorization": "Bearer tok"}}
    assert conf["mcp_servers"]["other"] == {"command": "y"} and conf["model"] == "x"
    assert (st.CODEX_TOML.parent / "config.toml.bak").exists()
    st._codex_remove([])
    assert "phone" not in tomllib.loads(st.CODEX_TOML.read_text())["mcp_servers"]


DSH = """- id: llm
  config:
    a: 1
- id: mcp-manager
  config:
    servers:
      # The resident phonectl service.
      phone:
        transport: streamable-http
        url: http://127.0.0.1:1/mcp
        headers:
          Authorization: "Bearer old"
      other:
        url: http://x
- id: ui-theme
  config:
    preference: light
"""


def test_deepseek_harness_entry_is_replaced_in_place(home):
    st.DSH_PATCH.parent.mkdir()
    st.DSH_PATCH.write_text(DSH)
    st._dsh_add(URL, TOKEN, [])
    st._dsh_add(URL, TOKEN, [])
    text = st.DSH_PATCH.read_text()
    assert text.count("      phone:") == 1 and URL in text and '"Bearer tok"' in text
    assert "Bearer old" not in text and "The resident phonectl service" in text  # replaced in place
    assert "      other:\n        url: http://x\n" in text and text.endswith("    preference: light\n")
    st._dsh_remove([])
    assert "phone:" not in st.DSH_PATCH.read_text() and "other:" in st.DSH_PATCH.read_text()


def test_deepseek_harness_without_an_mcp_manager_gets_one(home):
    st.DSH_PATCH.parent.mkdir()
    st.DSH_PATCH.write_text("- id: llm\n  config:\n    a: 1\n")
    st._dsh_add(URL, TOKEN, [])
    assert st.DSH_PATCH.read_text().startswith("- id: llm\n  config:\n    a: 1\n- id: mcp-manager\n  config:\n    servers:\n")


@pytest.fixture
def agent(home, monkeypatch):
    calls = []
    a = st.Agent("fake", "Fake agent", home / "skills", lambda: True,
                 lambda url, token, changed: calls.append(("add", url, token)),
                 lambda changed: calls.append(("remove",)), "reload it")
    monkeypatch.setitem(st.AGENTS, "fake", a)
    a.calls = calls
    return a


def test_install_update_and_remove_the_skill(agent, monkeypatch, tmp_path):
    out = st.install("fake", URL, TOKEN)
    skill = agent.skills / st.SKILL / "SKILL.md"
    assert skill.exists() and agent.calls == [("add", URL, TOKEN)] and "skill installed" in out[0]
    assert st.installed() == ["fake"] and st.stale() == []
    # A newer phonectl ships another skill: the installed one is now stale, and install updates it.
    newer = tmp_path / "newer" / st.SKILL
    newer.mkdir(parents=True)
    (newer / "SKILL.md").write_text("---\nname: phonectl-scripts\n---\nnew rules\n")
    monkeypatch.setattr(st, "SKILL_SRC", newer)
    assert st.stale() == ["Fake agent"]
    st.install("fake", URL, TOKEN)
    assert skill.read_text().endswith("new rules\n") and st.stale() == []
    assert (st.SKILL_BACKUPS / "fake" / st.SKILL / "SKILL.md").exists()  # the old copy, outside the skills folder
    st.uninstall("fake")
    assert not skill.parent.exists() and st.installed() == [] and agent.calls[-1] == ("remove",)


def test_a_skill_changed_by_hand_is_left_alone(agent):
    st.install("fake", URL, TOKEN)
    skill = agent.skills / st.SKILL / "SKILL.md"
    skill.write_text("my own notes\n")
    assert "left as it is" in st.install("fake", URL, TOKEN)[0]
    assert skill.read_text() == "my own notes\n" and st.stale() == []
    st.uninstall("fake")
    assert skill.exists()  # not removed either
    st.install("fake", URL, TOKEN, force=True)
    assert skill.read_text() != "my own notes\n"


def test_deepseek_harness_entries_indented_deeper_are_kept_apart(home):
    st.DSH_PATCH.parent.mkdir()
    st.DSH_PATCH.write_text("- id: mcp-manager  # servers\n  config:\n    servers:\n        other:\n"
                            "          url: http://x\n- id: ui-theme\n")
    st._dsh_add(URL, TOKEN, [])
    st._dsh_add(URL, TOKEN, [])  # phonectl update
    text = st.DSH_PATCH.read_text()
    assert "        other:\n          url: http://x\n" in text  # still its own entry
    assert text.count("        phone:\n") == 1 and "        other:" in text
    assert text.count("- id: mcp-manager") == 1
    st._dsh_remove([])
    assert "phone" not in st.DSH_PATCH.read_text() and "other:" in st.DSH_PATCH.read_text()


@pytest.mark.parametrize("text", [
    "- id: mcp-manager\n  config:\n    servers: {}\n",
    "- id: mcp-manager\n  config: {servers: {}}\n",
    "- id: mcp-manager\n  config:\n    servers:\n      phone: {url: x}\n",
    "- id: mcp-manager\n- id: mcp-manager\n",
])
def test_deepseek_harness_layouts_it_does_not_know_are_left_untouched(home, text):
    st.DSH_PATCH.parent.mkdir()
    st.DSH_PATCH.write_text(text)
    with pytest.raises(st.SetupError):
        st._dsh_add(URL, TOKEN, [])
    assert st.DSH_PATCH.read_text() == text


def test_the_first_backup_is_the_original(home):
    st.DSH_PATCH.parent.mkdir()
    st.DSH_PATCH.write_text(DSH)
    st._dsh_add(URL, TOKEN, [])
    st._dsh_add("http://127.0.0.1:1/other", TOKEN, [])
    assert (st.DSH_PATCH.parent / "cordis.patch.yml.bak").read_text() == DSH


def test_codex_keeps_the_comments_above_the_next_table(home):
    st.CODEX_TOML.parent.mkdir()
    st.CODEX_TOML.write_text('[mcp_servers.phone]  # ours\nurl = "old"\n\n# my notes on other\n[mcp_servers.other]\ncommand = "y"\n')
    st._codex_add(URL, TOKEN, [])
    text = st.CODEX_TOML.read_text()
    assert "# my notes on other\n[mcp_servers.other]" in text and "old" not in text


def test_a_broken_record_file_is_reported_not_overwritten(home):
    st.STATE.write_text("{not json")
    with pytest.raises(st.SetupError):
        st.installed()
    assert st.STATE.read_text() == "{not json"
