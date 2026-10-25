Find Best Images duplicates image files from one or more source directories to a target directory named "search-results". While duplicating it will try to identify similar images and try to retain the best quality image.

There is an option to generate a ".dup" placeholder when a different image was selected to be a better quality. The .dup file contains the path to the selected image file.

There is also an option to duplicate any other files it finds. It does not do any matching on these files.

Another option will cause this program to generate a ".bad" file for any image file that it was unable to read.

There is also a "dry run" option that will run through the scan, but not create any files at the target location.

While this program is scanning, it will prevent the computer from going to sleep.

You can adjust the matching threshold from 0 to 64. Lower values use stricter comparisons.

You can also add bonus points to images that use lossless image formats. If a JPG and PNG are deemed to be a match, this bonus would cause the PNG to be selected over the JPG. This does not guarantee the PNG will always be chosen, but it will make it more likely.
