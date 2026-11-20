/* All static data, allocated stacks, DMA slabs and configured heap precede
 * _image_ram_end. Keep 32 KiB unallocated in addition to those allocations.
 * This deliberately rejects durations that merely fit the PCM array alone.
 */
ASSERT(_image_ram_end + 32768 <= ORIGIN(RAM) + LENGTH(RAM),
       "Audio capture leaves less than 32 KiB spare RAM; shorten APP_CAPTURE_DURATION_MS")

