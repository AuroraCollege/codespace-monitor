# codespace-monitor

Live, read-only visibility into student source files from a teacher's
GitHub Codespace. The relay and dashboard use Python's standard library;
the student agent has no Python package dependencies.

## Teacher Server

1. Publish this repository as a **public** repository. The student agent
	fetches `server.json` from GitHub's public raw-content endpoint.
2. Create a Codespaces secret named `CLASS_CODE` and another named
	`TEACHER_KEY`. Use the same class code in the student template. These
	are shared secrets, not strong authentication.
3. Open this repository in a Codespace. The post-start hook starts the
	server on port 5000, attempts to set that port to Public, and writes
	the current public URL to `server.json` when both Codespaces URL
	variables are available.
4. Commit and push `server.json` after the first launch, and whenever the
	server Codespace is recreated. It contains only the public server URL.
5. Open the forwarded port URL with `/?key=YOUR_TEACHER_KEY`. Without
	the teacher key, dashboard assets and APIs return 403.

The server keeps student content in memory only. Restarting it clears
state; connected agents repopulate it with snapshots. It does not execute
student code or store snapshots on disk.

## Student Template

Copy `student-template/agent.py` and `student-template/.devcontainer`
into the student template repository. In its devcontainer configuration,
replace `OWNER/monitor-server` in `SERVER_DISCOVERY_URL` with this public
repository's owner and name. Make `CLASS_CODE` available to the student
Codespace as a Codespaces secret. `GITHUB_USER` is supplied by
Codespaces; the optional `STUDENT_DISPLAY_NAME` environment variable
overrides the dashboard label.

The agent starts automatically and saves its local status in its log at
`/tmp/student-monitor-agent.log`. At startup it prints a visible sharing
notice. It sends only allowlisted source and text/config files, and skips
the configured sensitive patterns, build/dependency folders, workspace
`.gitignore` matches, binaries, files over 200 KB, and snapshots over
300 files or 3 MB. Review the allowlist and exclusions in
`student-template/agent.py` before using a different curriculum or
workspace layout.

Suggested class/school notice:

> While the Student Monitor agent is running in this class Codespace,
> selected saved source and text files in your workspace are shared with
> your teacher for live progress support. Secrets, common dependency and
> build folders, ignored files, binary files, and oversized files are
> excluded. The server keeps the current view in memory and does not run
> your code. Please do not put passwords or personal information in
> classwork files.

The class code and teacher key are simple shared secrets. They are not
individual accounts and should not be treated as strong authentication.
The dashboard key is passed in the URL, so use it only on the teacher's
Codespace and avoid sharing that URL.

## Local Development

```sh
export CLASS_CODE='local-class-code'
export TEACHER_KEY='local-teacher-key'
python3 server.py
```

The dashboard uses Highlight.js and web fonts from public CDNs. Student
file contents stay on the monitor server, but the browser needs access to
those CDNs for syntax highlighting and the selected typefaces. Plain text
remains readable if highlighting is unavailable.

## Deployment Checks

- Verify `CODESPACE_NAME` and `GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN`
  are available during `postStartCommand`.
- Confirm public port visibility is allowed by the organisation and
  remains set after a Codespace restart.
- Confirm students can reach the forwarded URL and
  `raw.githubusercontent.com`.
- Check the teacher Codespace idle timeout and available Codespaces
  core-hours before a lesson.