# 🎮 Steam Profile URL Checker

![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python)
![Async](https://img.shields.io/badge/Asyncio-aiohttp-green)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

🔍 **Asynchronous Steam vanity/profile URL availability checker** built with Python.  
Check whether custom Steam profile URLs (vanity names) are **available or taken**, using your own wordlist or auto-generated words.

---

## ✨ Features

- 🚀 **Asynchronous checking** → high performance with `asyncio` + `aiohttp`.
- 📂 **Multiple input modes**:
  - `Custom.txt` → check usernames from your own list.
  - Random Word API → fetch thousands of words automatically.
- 📝 **Detailed logging**:
  - `results.csv` → full logs (username, status, HTTP code, timestamp).
  - `Available.txt` → only available usernames.
- 🔄 **Resume support** → skips already-checked usernames.
- ⏱️ **Progress display** with checked count, available count, elapsed time, and ETA.
