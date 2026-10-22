# Third-party components

Download Enforcer uses these unmodified runtime components:

- libtorrent-rasterbar 2.1.1: BSD license, copyright Arvid Norberg. [Source at v2.1.1](https://github.com/arvidn/libtorrent/tree/v2.1.1). The CPython 3.13 Windows x64 wheel was obtained from PyPI and its SHA-256 checked against the published value: `fa11aa801d5b9184f067f559c465d3074a4e878d9c671b00dd4930d6dfce2b46`.
- Qt for Python / PySide6 / Shiboken 6.11.1: used under the available LGPLv3 open-source option. [PySide source at v6.11.1](https://code.qt.io/cgit/pyside/pyside-setup.git/tree/?h=v6.11.1), [Qt source archives](https://download.qt.io/archive/qt/6.11/6.11.1/), [upstream notices](https://doc.qt.io/qtforpython-6/licenses.html), [Qt third-party attribution](https://doc.qt.io/qt-6/licenses-used-in-qt.html).
- CPython 3.13.15: Python Software Foundation license. [Source](https://www.python.org/downloads/release/python-31315/).
- Boost and OpenSSL license texts are included for native dependency notices.

The supplied `licenses` folder contains the libtorrent BSD, Boost, OpenSSL Apache-2.0, LGPLv3, GPLv3, and Python license texts, along with notices supplied by the installed PySide packages. Commercial-license references supplied by those packages describe an alternative option; this application uses the open-source licensing option.

Qt libraries are dynamically linked and distributed in a folder so compatible replacements can be used. Application source and build instructions accompany this local release. No changes have been made to Qt, PySide, Shiboken, libtorrent, or CPython. The Qt libraries' terms permit reverse engineering for debugging modifications to those libraries; this app adds no restriction against it.

The application icon was generated for this project using the built-in image-generation tool. The exact final prompt is in `assets/ICON-PROMPT.txt`.
