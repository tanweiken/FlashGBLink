# Test profile fixtures

These four `fc_AGB_*.txt` files are copied from FlashGBX's `config/`
directory for the hardware-free tests:

- `fc_AGB_M36L0R806.txt`
- `fc_AGB_GL256S.txt`
- `fc_AGB_Generic_AAA_AA.txt`
- `fc_AGB_29GL128F.txt`

FlashGBX is by Lesserkuma, licensed GPL-3.0-or-later:
https://github.com/Lesserkuma/FlashGBX

The plugin and the CLI use FlashGBX's installed profile database at runtime;
these copies exist only so the test suite can run without a FlashGBX install.
