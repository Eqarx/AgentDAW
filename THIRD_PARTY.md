# Source and license information

KontaktStudio's original Python and C++ code is available under GPL-3.0-only. A copy is in LICENSE. This package references the separately installed Native Instruments Kontakt 8 plugin and the user's instrument libraries. The demo JSON contains the note data used to produce the accompanying short audio example.

The Windows runtime contains upstream distributions. Their license and notice files remain under runtime/LICENSE.txt and runtime/packages/*dist-info. The following upstream projects provide the corresponding sources.

| Component | Version | Source |
| --- | --- | --- |
| CPython Windows embeddable runtime | 3.10.11 | [CPython source](https://github.com/python/cpython/tree/v3.10.11) |
| Pedalboard with JUCE | 0.9.26 | [Pedalboard source and submodules](https://github.com/spotify/pedalboard/tree/v0.9.26) |
| NumPy | 2.2.6 | [NumPy source](https://github.com/numpy/numpy/tree/v2.2.6) |
| Mido | 1.3.3 | [Mido source](https://github.com/mido/mido/tree/1.3.3) |
| packaging | 25.0 | [packaging source](https://github.com/pypa/packaging/tree/25.0) |
| fastjsonschema | 2.21.2 | [Source distribution](https://pypi.org/project/fastjsonschema/2.21.2/#files) |

The included drop.dll was built from native/drop.cpp with MinGW-w64 GCC. GCC runtime components use the GCC Runtime Library Exception. See the [GCC runtime exception](https://www.gnu.org/licenses/gcc-exception-3.1.html) and the [MinGW-w64 project](https://www.mingw-w64.org/).
