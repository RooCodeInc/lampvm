# Separate software and images

lampvm source is MIT licensed. It downloads, rather than commits, Tart 2.32.1.
Tart's Fair Source 0.9 licence is preserved in [Tart-2.32.1.txt](Tart-2.32.1.txt).
Its use limit is 100 CPU cores per entity (including affiliates); the exception
for devices used by a single individual is in that licence. Check the licence
before running a shared fleet beyond that limit. Keep the licence if distributing
Tart binaries. Preserve Tart's complete signed application bundle and its embedded
provisioning profile; it is the vendor's, not a user's signing credential.

Tart: https://github.com/openai/tart/releases/tag/2.32.1

Guest preparation follows Cirrus Labs' macOS image templates, including the
image's approach to allowing automation in a guest with SIP disabled:
https://github.com/cirruslabs/macos-image-templates

VM images and Apple's operating system are separate from this source licence.
Downloading a public base image does not confer permission to redistribute macOS
disk images. Distribute the source and setup instructions, not VM disks, NVRAM,
user state, or a prepared private image.
