**Find Best Images**  

Find Best Images will duplicate image files from one or more source directories to a target directory named "search-results". During the duplication process it will try to identify similar images and only keep the highest quality image.

This program defaults to a GUI interface, but can be run directly from the command line.

To use the GUI, execute the program. Select some source directories. Next, select a target directory. Adjust the options to your liking and click the Start Scan button. The program will scan the source directories and duplicate what it finds according to the settings you have chosen.

Options are:
- Create .dup files: When two or more images match, files ending in .dup will be created in the target directory as placeholders for the unkept images.
- Copy non-image files: This will copy files that are not images to the target directory, otherwise they are ignored.
- Create .bad files: If an image cannot be read, a file ending in .bad will be created as a placeholder in the target directory.
- Keep computer awake: This prevents the computer from going to sleep part way through a long scan.
- Treat images with different sizes: Only image files with the exact same file size will be selected for comparison.
- Dry run: This performs the scan, but does not create any files at the target location.
- Match threshold: This defines how close two images must match before they are considered the same. Higher numbers here will allow more images to match each other.
- Lossless bonus: Some image types do not lose detail when they are created. You can increase the chances of a lossless image being selected over a lossy image by increasing this value.

Command line options are:  
--no-gui : Do not use the GUI  
-s / --source *DIR* [*DIR*] :  One or more source directories to scan  
-t / --target *DIR* :  Directory to store the search results  
--no-subdirs :  Do not scan subdirectories found in the source directories  
--no-dup :  Do not create .dup image placeholders  
--no-bad-files :  Do not create .bad image placeholders  
--no-copy-other : Do not copy files that are not images  
--allow-sleep :  Allow the computer to sleep during the scan  
--require-same-size :  Images of different file sizes are considered to be different  
-n / --dry-run :  Perform the scan but do not duplicate the files  
-T / --threshold *BITS* : Match threshold  (default 5)
-lossless-bonus *X* : Bonus to apply to an image using a lossless format  (default 1.10)
--ratio-tol *X* : Aspect ratio tolerance when comparing images  (default 0.03)
-j / --jobs *X* : Number of worker processes to use  (default *CPU count*)
-p / --progress : Show a progress bar when run from the command line  
-v / --verbose : Verbose output when run from the command line  
-h / --help : View the command help  

See the help file in the GUI for more details.

The following file types are considered to be images:  png, .jpg, .jpeg, .webp, .bmp, .gif, .tif, .tiff

If a match is found between two different sources, and the match is a tie, the image in the first source will be selected.

**This program was developed with the assistance of Claude AI**
