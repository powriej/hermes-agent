"""Regression tests for sudo detection and sudo password handling."""

import tools.terminal_tool as terminal_tool


def setup_function():
    terminal_tool._reset_cached_sudo_passwords()


def teardown_function():
    terminal_tool._reset_cached_sudo_passwords()


def test_searching_for_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "rg --line-number --no-heading --with-filename 'sudo' . | head -n 20"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_terminal_schema_advertises_persistent_env_state():
    description = terminal_tool.TERMINAL_TOOL_DESCRIPTION

    assert "exported environment variables persist between calls" in description
    assert "activate a virtualenv" in description
    assert "do not re-source the same environment before every command" in description


def test_printf_literal_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "printf '%s\\n' sudo"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_non_command_argument_named_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "grep -n sudo README.md"
    transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None


def test_actual_sudo_command_uses_configured_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    # Pin the "sudo will prompt" branch. _transform_sudo_command now probes host
    # sudo state even when SUDO_PASSWORD is set (a NOPASSWD host or live
    # timestamp reads nothing from stdin, so the password would leak to the next
    # command in the pipeline). Without this the assertion below depends on the
    # developer's own sudoers config.
    monkeypatch.setattr(terminal_tool, "_sudo_nopasswd_works", lambda: False)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo apt install -y ripgrep")

    assert transformed == "sudo -S -p '' apt install -y ripgrep"
    assert sudo_stdin == "testpass\n"


def test_explicit_empty_sudo_password_tries_empty_without_prompt(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    # See the note above: pin the prompting branch so the host's sudoers config
    # cannot decide this test's outcome.
    monkeypatch.setattr(terminal_tool, "_sudo_nopasswd_works", lambda: False)

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError("interactive sudo prompt should not run for explicit empty password")

    monkeypatch.setattr(terminal_tool, "_prompt_for_sudo_password", _fail_prompt)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo true")

    assert transformed == "sudo -S -p '' true"
    assert sudo_stdin == "\n"


def test_validate_workdir_blocks_shell_metacharacters_in_windows_paths():
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project; rm -rf /")
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project$(whoami)")
    assert terminal_tool._validate_workdir("C:\\Users\\Alice\\project\nwhoami")


def test_count_real_sudo_invocations_ignores_mentions(monkeypatch):
    assert terminal_tool._count_real_sudo_invocations("grep sudo README.md") == 0
    assert terminal_tool._count_real_sudo_invocations("sudo a; sudo b") == 2


# ── sudo password must never land where sudo won't read it ──────────────────
# sudo_stdin is piped to the SHELL, not to sudo. sudo -S consumes exactly one
# line only when it actually prompts; when it does not, the line falls through
# to whatever runs next and the operator's password comes back as command
# output (model context, transcript, logs). SUDO_PASSWORD is deliberately kept
# out of the child environment (environments/local.py), so emitting it on a
# shared stdin defeats that scrubbing.


def _no_nopasswd(monkeypatch):
    """Force the 'sudo would prompt' branch so only the -n logic is exercised."""
    monkeypatch.setattr(terminal_tool, "_sudo_nopasswd_works", lambda: False)


def test_noninteractive_sudo_does_not_receive_a_password_line(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "hunter2")
    _no_nopasswd(monkeypatch)

    # `sudo -n` fails rather than prompting, so it reads nothing; `cat` would
    # otherwise echo the password straight back to the model.
    for command in (
        "sudo -n true; cat",
        "sudo --non-interactive true; cat",
        "sudo -kn true; cat",
        "sudo -p 'pw: ' -n true; cat",
    ):
        transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)
        assert sudo_stdin is None, f"password injected for {command!r}"
        assert transformed == command, f"command rewritten for {command!r}"


def test_prompting_sudo_still_receives_its_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "hunter2")
    _no_nopasswd(monkeypatch)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo apt-get update")
    assert transformed == "sudo -S -p '' apt-get update"
    assert sudo_stdin == "hunter2\n"

    # one line per invocation is preserved for compound commands
    _, compound_stdin = terminal_tool._transform_sudo_command("sudo a && sudo b")
    assert compound_stdin == "hunter2\nhunter2\n"


def test_child_command_n_flag_is_not_mistaken_for_sudo_n(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "hunter2")
    _no_nopasswd(monkeypatch)

    # -n here belongs to apt-get/tar, not sudo, so sudo still prompts.
    for command in ("sudo apt-get install -n foo", "sudo tar -xn archive"):
        _, sudo_stdin = terminal_tool._transform_sudo_command(command)
        assert sudo_stdin == "hunter2\n", f"password wrongly withheld for {command!r}"


def test_nopasswd_host_gets_no_password_line_even_when_configured(monkeypatch):
    """A live sudo timestamp / NOPASSWD sudoers means sudo reads nothing."""
    monkeypatch.setenv("SUDO_PASSWORD", "hunter2")
    monkeypatch.setattr(terminal_tool, "_sudo_nopasswd_works", lambda: True)

    transformed, sudo_stdin = terminal_tool._transform_sudo_command("sudo true && cat")
    assert sudo_stdin is None
    assert transformed == "sudo true && cat"
