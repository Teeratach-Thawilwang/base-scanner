---
name: playwright
description: How to drive a browser through the playwright MCP in this repo, which tool for which need, and the traps. Load it when a browser is genuinely required, either a bug that only reproduces in the UI or a check the user has permitted. Not for E2E suites (write automated tests) and not for a UI question the user could answer faster.
---

# Playwright browser rules

**Off by default.** Use a browser only when the user permits it, or when a specific bug needs one.

- **E2E** → write automated tests, not a browser loop.
- **A UI check** → hand the user a numbered click-through scenario, their answer is faster and more accurate than an automated pass.

## Which tool for which need

| Need                               | Use                                                                                                                                                                     |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| click / fill / select              | native locator tools: `browser_click`, `browser_fill_form`, `browser_select_option`. **Never `browser_evaluate`**, a JS DOM click doesn't fire React's synthetic events |
| verify state                       | `browser_snapshot`                                                                                                                                                      |
| read one value (text, count, flag) | `browser_evaluate`, reading state is its only job                                                                                                                       |
| visual bug (layout, CSS, pixels)   | `browser_take_screenshot`, it costs 10 to 20 times a snapshot, so nothing else justifies it                                                                             |
| login                              | [Log in once, not once per session](#log-in-once-not-once-per-session), on credentials from [.claude/account/](../../account/) as `<project>.md`                        |

## Log in once, not once per session

The MCP runs a **persistent browser profile**: the config carries no `--isolated`, so Chrome writes its cookie store to disk and reloads it on the next run. A login survives the session that made it, until the cookie itself expires. Logging in again costs turns and buys nothing.

- **Check whether you are already signed in before you log in.** Navigate to a page behind auth and snapshot, a login form means the cookie expired, anything else means you are in.
- **Log in with the native tools**, `browser_fill_form` then `browser_click`, on credentials read at runtime from [.claude/account/](../../account/), the file named for the project.
- **Never add `--isolated` or `--storage-state` to the MCP config.** `--isolated` keeps the profile in memory and throws the login away every run, and `--storage-state` only seeds an isolated session, so the pair trades a login that already persists for one you have to script.
- **The profile went stale → delete it, then navigate again.** On Windows it is the `ms-playwright-mcp` folder under `%LOCALAPPDATA%`, and every OS is listed in the [playwright-mcp README](https://github.com/microsoft/playwright-mcp/blob/main/README.md).

## E2E: the login belongs to the test runner

Never drive a login through the MCP to set up a test. Playwright signs in once per run and hands that state to every project, so a suite of any size costs one login and zero turns.

- **A `setup` project does the login**, matching `*.setup.ts`, driving the page API, ending on `page.context().storageState({ path: 'playwright/.auth/user.json' })`.
- **Every other project reads that state**, taking `use: { storageState: 'playwright/.auth/user.json' }` plus `dependencies: ['setup']`.
- **That state file is a live session**, so it goes in the project's own `.gitignore`, never into a commit.

## Running a pass

- **Load the Playwright schemas once per session**, through ToolSearch, not once per step.
- **Plan the whole click sequence before opening the browser**, then run it in one pass.
- **Snapshot at checkpoints, never after every action:** fill all fields → submit → one snapshot.
