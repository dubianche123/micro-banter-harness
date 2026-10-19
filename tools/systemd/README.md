# Linux user service

Generate paths with the installed Python (no root/system power changes):

```sh
.venv/bin/python -m regular.service systemd --output data/the-regular.service
mkdir -p ~/.config/systemd/user
cp data/the-regular.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start the-regular
# Optional, explicitly enable on future logins:
systemctl --user enable the-regular
systemctl --user status the-regular
journalctl --user -u the-regular
```

Run `regular doctor` first. `.env` is loaded by the application from the generated
working directory, rather than relying on shell-specific export syntax. To keep
the user service alive after logout, configure user lingering with your system
administrator. The generator does not install, enable or start anything.
