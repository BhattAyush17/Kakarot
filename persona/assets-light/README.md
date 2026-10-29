# Lightweight built-in sprites

These sprite sheets are 25% resolution derivatives of the built-in 512-pixel-cell sheets. The overlay displays the pet at roughly 282 pixels, so these 128-pixel source cells trade some sharpness for a much smaller decoded-memory footprint. Custom personas continue to use their own assets.

Regenerate the files from `persona/assets/*.webp` with ImageMagick using 25% resize and WebP quality 82 when the source persona changes.
