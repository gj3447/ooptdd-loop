"""RED-first repair of the loop guards the 2026-07-15 review rejected.

Each test proves the defect against the unfixed code (so it is RED before the fix) and is
revert-proof: undo the production edit and the matching assertion goes RED again. The
common thread is that the guards were only ever exercised at their two extremes — full
scrub vs `'*'`, no journal vs an explicit run_id — and every bug lived in the middle.

- FIX-1  ``--fix-env-allow`` REPLACED ``harness.DEFAULT_ENV_ALLOWLIST`` instead of
         extending it, so the documented migration (``--fix-env-allow ANTHROPIC_API_KEY``)
         handed the fix an env with no PATH: the shell could not find the agent and the fix
         died with 127 before it ever read the credential. The API form
         (``env_allowlist=[*DEFAULT_ENV_ALLOWLIST, "ANTHROPIC_API_KEY"]``) and the CLI form
         were documented as the same migration, and were not.
- FIX-3  ``resume=True`` with neither ``run_id`` nor ``cid`` keyed the resume on a FRESH
         random cid, which can never match a journal line. The resume silently restarted at
         pass 1 and repaid the agent, reporting ``resumed=[False, ...]`` against a journal
         that plainly had lines.
- FIX-5  ``cli._cmd_run`` caught only ValueError, so a journal that cannot be replayed or
         cannot be written reached the production entrypoint as a raw traceback.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from ooptdd.backends import memory_reset
from ooptdd_loop import cli
from ooptdd_loop.harness import DEFAULT_ENV_ALLOWLIST, INHERIT_ALL
from ooptdd_loop.runner import run_until_complete
from ooptdd_loop.domain.spec import load_spec

RED_BODY = "def run(backend, cid):\n    pass\n"


@pytest.fixture(autouse=True)
def _clean():
    memory_reset()
    yield
    memory_reset()


def _modname(tmp_path) -> str:
    # unique per test so the import cache never serves another test's target module
    return "svc_" + os.path.basename(str(tmp_path)).replace("-", "_")


def _make(tmp_path) -> str:
    """A one-requirement spec whose target never ships `ping`, i.e. stays RED. These tests
    are about the loop's plumbing, not about converging it — nothing here should go GREEN."""
    mod = _modname(tmp_path)
    (tmp_path / f"{mod}.py").write_text(RED_BODY, encoding="utf-8")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(f"""
target:
  mode: in_process
  callable: {mod}:run
  backend: memory
  root: {tmp_path}
requirements:
  - id: REQ-PING
    description: a ping is emitted
    gate: [{{event: ping, op: "==", count: 1}}]
    longinus: {{kg_anchor: 'ref:ping', source: {mod}.py, symbol: run, must_emit: ping}}
""")
    return str(spec_path)


def _counting_fix(tmp_path) -> tuple[str, object]:
    """An 'agent' that never fixes anything but records every time it was paid for."""
    counter = tmp_path / "calls.txt"
    script = tmp_path / "count.py"
    script.write_text(
        f"import pathlib; p = pathlib.Path(r{str(counter)!r}); "
        "p.write_text(str(int(p.read_text()) + 1 if p.exists() else 1))\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {script}", counter


# ── FIX-1 (blocker): the documented CLI migration must actually RUN the fix ───
def _path_dependent_agent(tmp_path) -> tuple[str, str, object]:
    """An 'agent' that can only be found if PATH survived the scrub, and that reports the
    env it was handed.

    Invoked by BARE NAME: the kernel resolves the ``#!/bin/sh`` shebang, but the shell must
    resolve the *name* itself, which is what needs PATH. Its body is shell builtins and a
    redirection only, so nothing inside it needs PATH either — a 127 here means exactly one
    thing: the fix's env had no usable PATH. This is what a real agent invocation
    (``claude -p …``, ``codex exec …``) looks like, and what the two existing extremes
    (full scrub / ``'*'``) never exercised.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    saw = tmp_path / "agent_saw.txt"
    agent = bindir / "ooptdd-fake-agent"
    agent.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n%s\\n%s\\n\' "${ANTHROPIC_API_KEY:-MISSING}" '
        '"${OPENAI_API_KEY:-MISSING}" "${PATH:-MISSING}" > ' + str(saw) + "\n",
        encoding="utf-8",
    )
    agent.chmod(0o755)
    return str(bindir), "ooptdd-fake-agent", saw


def test_cli_documented_env_migration_actually_executes_the_fix(tmp_path, monkeypatch, capsys):
    # THE blocker. AGENT_LOOP.md documents exactly this line as the migration for a fix
    # that needs a credential. Before the repair it produced fix_exit=127 — the shell could
    # not find the agent, because --fix-env-allow REPLACED the defaults and took PATH with
    # it — so the loop reported a fix that "ran" and had in fact never started.
    bindir, agent, saw = _path_dependent_agent(tmp_path)
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-migration")
    rc = cli.main(["run", _make(tmp_path), "--passes", "2", "--patience", "5", "--json",
                   "--fix", agent, "--fix-env-allow", "ANTHROPIC_API_KEY"])
    assert rc == 1                       # the SUT stays RED: this agent fixes nothing
    payload = json.loads(capsys.readouterr().out)
    ran = [p for p in payload["transcript"] if p["fix_ran"]]
    assert ran, "the loop never invoked the fix at all"
    assert ran[0]["fix_exit"] == 0       # 127 = the shell could not find the agent
    anthropic, _openai, path = saw.read_text().splitlines()
    assert anthropic == "sk-migration"   # ...and the credential the flag named arrived
    assert path != "MISSING"


def test_cli_env_allow_still_scrubs_the_credentials_it_was_not_asked_for(tmp_path, monkeypatch,
                                                                        capsys):
    # the other direction: "extend" must not quietly become "inherit everything". Asking
    # for one credential must pass one credential.
    bindir, agent, saw = _path_dependent_agent(tmp_path)
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-asked-for")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")
    cli.main(["run", _make(tmp_path), "--passes", "2", "--patience", "5", "--json",
              "--fix", agent, "--fix-env-allow", "ANTHROPIC_API_KEY"])
    capsys.readouterr()
    anthropic, openai, _path = saw.read_text().splitlines()
    assert anthropic == "sk-asked-for"
    assert openai == "MISSING"


def test_cli_env_allow_default_scrub_is_unchanged_and_the_fix_still_runs(tmp_path, monkeypatch,
                                                                        capsys):
    # no flag at all: the default allowlist still carries PATH (so the agent is findable)
    # and still drops every credential.
    bindir, agent, saw = _path_dependent_agent(tmp_path)
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    rc = cli.main(["run", _make(tmp_path), "--passes", "2", "--patience", "5", "--json",
                   "--fix", agent])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert [p for p in payload["transcript"] if p["fix_ran"]][0]["fix_exit"] == 0
    anthropic, _openai, path = saw.read_text().splitlines()
    assert anthropic == "MISSING" and path != "MISSING"


def test_cli_inherit_all_sentinel_survives_the_extend(tmp_path, monkeypatch, capsys):
    # '*' must stay an opt-OUT after the extend. This guards the CLI's whole inherit-all
    # path; the SHAPE of what _fix_env_allowlist returns for '*' is pinned by the unit test
    # below instead, since fix_env matches the sentinel by membership and would inherit-all
    # either way.
    bindir, agent, saw = _path_dependent_agent(tmp_path)
    monkeypatch.setenv("PATH", bindir + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("OPENAI_API_KEY", "sk-opted-in")
    cli.main(["run", _make(tmp_path), "--passes", "2", "--patience", "5", "--json",
              "--fix", agent, "--fix-env-allow", INHERIT_ALL])
    capsys.readouterr()
    _anthropic, openai, _path = saw.read_text().splitlines()
    assert openai == "sk-opted-in"


def test_fix_env_allowlist_extends_rather_than_replaces_the_defaults():
    # the unit under the three e2e tests above.
    assert cli._fix_env_allowlist(None) is None          # no flag => the default scrub
    assert cli._fix_env_allowlist([]) is None
    assert cli._fix_env_allowlist(["ANTHROPIC_API_KEY"]) == [*DEFAULT_ENV_ALLOWLIST,
                                                            "ANTHROPIC_API_KEY"]
    assert cli._fix_env_allowlist([INHERIT_ALL]) == [INHERIT_ALL]          # opt-out, untouched
    assert cli._fix_env_allowlist(["FOO", INHERIT_ALL]) == ["FOO", INHERIT_ALL]


# ── FIX-3 (blocker): a resume keyed on a fresh cid can never match ────────────
def test_resume_without_a_stable_run_id_is_a_config_error(tmp_path):
    # run_id defaults to the cid, and the cid defaults to a fresh UUID. So this resume was
    # keyed on an identity no journal line could ever carry.
    journal = tmp_path / "run.jsonl"
    spec = load_spec(_make(tmp_path))
    run_until_complete(spec, max_passes=2, fix_cmd="true", patience=5,
                       journal_path=journal, run_id="RUN-F")
    with pytest.raises(ValueError, match="stable run_id"):
        run_until_complete(spec, max_passes=4, fix_cmd="true", patience=5,
                           journal_path=journal, resume=True)


def test_resume_with_no_journal_still_reports_the_journal_first(tmp_path):
    # both config errors apply to `resume=True` alone; the missing journal is the one that
    # names the nearer cause, so it must keep winning.
    with pytest.raises(ValueError, match="journal_path"):
        run_until_complete(load_spec(_make(tmp_path)), resume=True)


def test_resume_never_silently_repays_the_agent_against_a_journal_with_lines(tmp_path, capsys):
    # the fail-open, end to end: before the repair this returned 1, paid the agent a second
    # time, and reported resumed=[False, ...] for a journal that plainly had lines.
    journal = tmp_path / "run.jsonl"
    fix_cmd, counter = _counting_fix(tmp_path)
    spec_path = _make(tmp_path)
    assert cli.main(["run", spec_path, "--passes", "2", "--patience", "5", "--fix", fix_cmd,
                     "--journal", str(journal), "--run-id", "RUN-G"]) == 1
    capsys.readouterr()
    assert int(counter.read_text()) == 1
    assert len(journal.read_text().splitlines()) == 2        # the journal HAS lines

    rc = cli.main(["run", spec_path, "--passes", "4", "--patience", "5", "--fix", fix_cmd,
                   "--journal", str(journal), "--resume"])   # no --run-id: the fail-open
    assert rc == 2                                           # config error, not a silent re-run
    assert "stable run_id" in capsys.readouterr().err
    assert int(counter.read_text()) == 1                     # the agent was NOT paid again


def test_resume_with_an_explicit_run_id_still_replays_the_journal(tmp_path, capsys):
    # the direction the guard must not break: a real resume still marks the paid passes
    # resumed=True and starts at the next unpaid one.
    journal = tmp_path / "run.jsonl"
    spec_path = _make(tmp_path)
    cli.main(["run", spec_path, "--passes", "2", "--patience", "5", "--fix", "true",
              "--journal", str(journal), "--run-id", "RUN-H"])
    capsys.readouterr()
    rc = cli.main(["run", spec_path, "--passes", "3", "--patience", "9", "--fix", "true",
                   "--journal", str(journal), "--run-id", "RUN-H", "--resume", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    resumed = [p["resumed"] for p in payload["transcript"]]
    assert resumed[:2] == [True, True]     # never [False, ...] against a journal with lines
    assert True in resumed


def test_resume_keyed_on_a_caller_supplied_cid_is_accepted(tmp_path, capsys):
    # run_id defaults to the cid — which is stable when the CALLER pinned it, so it can
    # match a journal line. Only a cid the loop invented is the fail-open.
    journal = tmp_path / "run.jsonl"
    spec_path = _make(tmp_path)
    cli.main(["run", spec_path, "--passes", "2", "--patience", "5", "--fix", "true",
              "--journal", str(journal), "--cid", "pinned-cid"])
    capsys.readouterr()
    rc = cli.main(["run", spec_path, "--passes", "3", "--patience", "9", "--fix", "true",
                   "--journal", str(journal), "--cid", "pinned-cid", "--resume", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert [p["resumed"] for p in payload["transcript"]][:2] == [True, True]


def test_resume_with_a_WRONG_run_id_still_restarts_at_pass_1_documented_gap(tmp_path, capsys):
    # NOT a fix — the honest boundary of the one above, pinned so it cannot drift unnoticed.
    # The guard closes a MISSING identity. A mistyped one matches no journal line either and
    # silently restarts at pass 1, because that is indistinguishable from a run that crashed
    # before its first pass completed (a resume that MUST start at pass 1). AGENT_LOOP.md
    # says so, and `resumed` is the tell.
    journal = tmp_path / "run.jsonl"
    fix_cmd, counter = _counting_fix(tmp_path)
    spec_path = _make(tmp_path)
    cli.main(["run", spec_path, "--passes", "2", "--patience", "5", "--fix", fix_cmd,
              "--journal", str(journal), "--run-id", "RUN-REAL"])
    capsys.readouterr()
    assert int(counter.read_text()) == 1

    rc = cli.main(["run", spec_path, "--passes", "2", "--patience", "5", "--fix", fix_cmd,
                   "--journal", str(journal), "--run-id", "RUN-TYPO", "--resume", "--json"])
    assert rc == 1                                        # no error is raised: the gap
    payload = json.loads(capsys.readouterr().out)
    assert [p["resumed"] for p in payload["transcript"]] == [False, False]  # repaid from 1
    assert int(counter.read_text()) == 2                  # the agent WAS paid again


# ── FIX-5: the production entrypoint must not emit a raw traceback ────────────
def test_a_corrupt_journal_is_a_clean_config_error_not_a_traceback(tmp_path, capsys):
    journal = tmp_path / "run.jsonl"
    journal.write_text('{"run_id": "RUN-I", "pass_no": 1}\n{"nope": 1}\n', encoding="utf-8")
    rc = cli.main(["run", _make(tmp_path), "--passes", "2", "--fix", "true",
                   "--journal", str(journal), "--run-id", "RUN-I", "--resume"])
    assert rc == 2                                   # not a JournalCorruptionError traceback
    err = capsys.readouterr().err
    assert "ooptdd-loop:" in err and "not a journal entry" in err


def test_an_unwritable_journal_is_a_clean_config_error_not_a_traceback(tmp_path, capsys):
    # the journal's parent exists as a FILE, so the first append raises OSError from inside
    # the loop — at the production entrypoint, that was a raw traceback.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory\n", encoding="utf-8")
    rc = cli.main(["run", _make(tmp_path), "--passes", "1", "--fix", "true",
                   "--journal", str(blocker / "run.jsonl"), "--run-id", "RUN-J"])
    assert rc == 2
    assert "ooptdd-loop:" in capsys.readouterr().err


def test_a_writable_journal_is_untouched_by_the_new_error_handling(tmp_path, capsys):
    # the other direction: the ordinary journaled run still reports its real verdict, not 2.
    journal = tmp_path / "nested" / "run.jsonl"
    rc = cli.main(["run", _make(tmp_path), "--passes", "1", "--fix", "true",
                   "--journal", str(journal), "--run-id", "RUN-K"])
    assert rc == 1                                   # RED, and RED is not a config error
    capsys.readouterr()
    assert len(journal.read_text().splitlines()) == 1
