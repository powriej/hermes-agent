"""Regression tests for sudo detection and sudo password handling."""

import tools.terminal_tool as terminal_tool
import tools.terminal_tool_sudo as terminal_tool_sudo


def setup_function():
    terminal_tool_sudo._reset_cached_sudo_passwords()


def teardown_function():
    terminal_tool_sudo._reset_cached_sudo_passwords()


def test_searching_for_sudo_does_not_trigger_rewrite(monkeypatch):
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    command = "rg --line-number --no-heading --with-filename 'sudo' . | head -n 20"
    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command(command)

    assert transformed == command
    assert sudo_stdin is None








def test_actual_sudo_command_uses_configured_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command("sudo apt install -y ripgrep")

    assert transformed == "sudo -S -p '' apt install -y ripgrep"
    assert sudo_stdin == "testpass\n"


def test_explicit_empty_sudo_password_tries_empty_without_prompt(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError("interactive sudo prompt should not run for explicit empty password")

    monkeypatch.setattr(terminal_tool_sudo, "_prompt_for_sudo_password", _fail_prompt)

    transformed, sudo_stdin = terminal_tool_sudo._transform_sudo_command("sudo true")

    assert transformed == "sudo -S -p '' true"
    assert sudo_stdin == "\n"


def test_headless_sudo_never_runs_backend_nopasswd_probe(monkeypatch):
    """No prompt can fire without a UI, so the backend round trip must not be paid."""
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    terminal_tool.set_sudo_password_callback(None)

    def _fail_probe():
        raise AssertionError("headless sudo must not probe the backend")

    assert terminal_tool_sudo._transform_sudo_command("sudo true", sudo_nopasswd_check=_fail_probe) == (
        "sudo true", None)


def test_validate_workdir_blocks_shell_metacharacters_in_windows_paths():
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project; rm -rf /")
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project$(whoami)")
    assert terminal_tool._validate_workdir("C:\\Users\\Alice\\project\nwhoami")


def test_validate_workdir_allows_unicode_filesystem_paths():
    assert terminal_tool._validate_workdir(
        "/Users/alice/Documents/Obs_Hermes_Data/项目-projects/客户拜访"
    ) is None
    assert terminal_tool._validate_workdir("/tmp/テスト") is None
    assert terminal_tool._validate_workdir("/home/jürgen/über projekt") is None


def test_validate_workdir_still_blocks_metachars_in_unicode_paths():
    # Widening to Unicode letters must not open the injection boundary:
    # shell metacharacters and control chars stay rejected even when mixed
    # with non-ASCII path segments.
    assert terminal_tool._validate_workdir("/tmp/テスト; rm -rf /")
    assert terminal_tool._validate_workdir("/tmp/项目$(whoami)")
    assert terminal_tool._validate_workdir("/tmp/über`id`")
    assert terminal_tool._validate_workdir("/tmp/テスト\nwhoami")
    assert terminal_tool._validate_workdir("/tmp/项目|cat /etc/passwd")
    assert terminal_tool._validate_workdir("/tmp/ü\x00ber")


def test_literal_sudo_executables_receive_password_stdin(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    for prefix in ("", "VAR='a b' ", "env ", "'/usr/bin/env' -i -u UNUSED X=1 ",
                   "env --unset=UNUSED --chdir /tmp -- X=1 ", "env -uUNUSED -C/tmp "):
        for executable in ("sudo", "/usr/bin/sudo", "'/opt/my tools/sudo'", '"/usr/bin/sudo"'):
            command = prefix + executable + " -u root true"
            rewritten, stdin = terminal_tool_sudo._transform_sudo_command(command)
            assert rewritten == prefix + executable + " -S -p '' -u root true"
            assert stdin == "testpass\n"


def test_sudo_rewrite_preserves_env_operands_and_prose(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")
    commands = (
        "echo '/usr/bin/sudo true'", "env echo sudo true", "env -u sudo echo ok",
        "env --chdir sudo echo ok", "env --unset=sudo echo ok", "env -- sudo=1 echo ok",
        ">/tmp/sudo echo ok", "env 2>/tmp/sudo echo ok", "env > /tmp/sudo echo ok",
        "/tmp/{a,b}/sudo true", "env X=1 -u UNUSED sudo", "env - -u UNUSED sudo", "env echo /usr/bin/sudo", "/tmp/*/sudo true",
        "env -S 'sudo true'", "env --unknown sudo true", "env --help sudo",
        "bash -c 'sudo true'", "echo ok # prose; /usr/bin/sudo true",
        '"/usr/bin/sudo', "env -u sudo", "env X=sudo", '"X=1" /usr/bin/sudo true',
    )
    for command in commands:
        assert terminal_tool_sudo._transform_sudo_command(command) == (command, None)


def test_count_real_sudo_invocations_ignores_mentions(monkeypatch):
    assert terminal_tool_sudo._count_real_sudo_invocations("grep sudo README.md") == 0
    assert terminal_tool_sudo._count_real_sudo_invocations("sudo a; sudo b") == 2


# ── the sudo password must never land where sudo won't read it ──────────────
# sudo_stdin goes to the SHELL's stdin, not sudo's. ``sudo -S`` consumes exactly one line and
# only when it actually prompts; otherwise the line falls through to the next reader in the
# command and the operator's password comes back as tool output.

_PROMPTS = {"sudo_nopasswd_check": lambda: False}
_NEVER_PROMPTS = {"sudo_nopasswd_check": lambda: True}


def test_non_prompting_backend_gets_no_password_line_even_when_configured(monkeypatch):
    """NOPASSWD sudoers or a live sudo timestamp: sudo reads nothing, so `cat` would echo it."""
    monkeypatch.setenv("SUDO_PASSWORD", "hunter2")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    assert terminal_tool_sudo._transform_sudo_command("sudo true && cat", **_NEVER_PROMPTS) == (
        "sudo true && cat", None)


def test_non_prompting_backend_gets_no_cached_password_line(monkeypatch):
    """An interactively entered password is cached; the timestamp it just warmed means no prompt."""
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    terminal_tool_sudo._set_cached_sudo_password("hunter2")

    assert terminal_tool_sudo._transform_sudo_command("sudo true; cat", **_NEVER_PROMPTS) == (
        "sudo true; cat", None)


def test_noninteractive_sudo_never_receives_a_password_line(monkeypatch):
    """`sudo -n` fails instead of prompting, whatever the host's sudo state: a property of the command."""
    monkeypatch.setenv("SUDO_PASSWORD", "hunter2")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    for command in (
        "sudo -n true; cat",
        "sudo --non-interactive true; cat",
        "sudo -kn true; cat",
        "sudo -p 'pw: ' -n true; cat",
        "sudo -u root -n true; cat",
        "sudo --user root -n true; cat",
        "sudo --user=root -n true; cat",
        "sudo -uroot -n true; cat",
        "/usr/bin/sudo -n true; cat",
        "env FOO=1 sudo -n true; cat",
        "true && sudo -n true | cat",
        "sudo apt-get update && sudo -n true; cat",
    ):
        assert terminal_tool_sudo._transform_sudo_command(command, **_PROMPTS) == (command, None), command


def test_noninteractive_sudo_is_not_prompted_for_interactively(monkeypatch):
    """Prompting the user for a password that `sudo -n` will never read is how it would leak."""
    monkeypatch.delenv("SUDO_PASSWORD", raising=False)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def _fail_prompt(*_args, **_kwargs):
        raise AssertionError("sudo -n must not trigger the interactive password prompt")

    monkeypatch.setattr(terminal_tool_sudo, "_prompt_for_sudo_password", _fail_prompt)

    assert terminal_tool_sudo._transform_sudo_command("sudo -n true; cat", **_PROMPTS) == (
        "sudo -n true; cat", None)


def test_prompting_sudo_still_receives_its_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "hunter2")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    assert terminal_tool_sudo._transform_sudo_command("sudo apt-get update", **_PROMPTS) == (
        "sudo -S -p '' apt-get update", "hunter2\n")
    # One line per invocation is preserved for compound commands.
    assert terminal_tool_sudo._transform_sudo_command("sudo a && sudo b", **_PROMPTS)[1] == "hunter2\nhunter2\n"


def test_n_that_is_not_sudos_own_flag_does_not_withhold_the_password(monkeypatch):
    monkeypatch.setenv("SUDO_PASSWORD", "hunter2")
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)

    for command in (
        "sudo apt-get install -n foo",   # the child command's flag
        "sudo tar -xn archive",
        "sudo -u nobody true",           # an option VALUE containing "n"
        "sudo -unobody true",
        "sudo -g nogroup -- ls -n",
        "sudo true; echo sudo -n",       # prose after a real sudo
    ):
        assert terminal_tool_sudo._transform_sudo_command(command, **_PROMPTS)[1] == "hunter2\n", command
