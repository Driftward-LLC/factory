# Hermes selection fixture provenance

`hermes-kill-all-818c13be.py` contains `ProcessRegistry.kill_all` from
[NousResearch/hermes-agent, commit 818c13be1dc4fd28987e1e881a9408224afd4535](https://github.com/NousResearch/hermes-agent/blob/818c13be1dc4fd28987e1e881a9408224afd4535/tools/process_registry.py).
The complete original `tools/process_registry.py` has SHA256
`4dacd3f6753d7b81e4ab338f7612624ec1f11c074cc969db49024a967a3b625e`.
Extraction retains exactly that method's source and removes its four-space class
indentation, with attribution comments prepended. The test pins the fixture's
SHA256 and executes only its parsed function, with a synthetic callback bounded
to disposable `Popen` child handles. It imports no upstream modules or recovery
functions and makes no claim about actual native termination or production wiring.

The excerpt is licensed under the upstream MIT License:

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
