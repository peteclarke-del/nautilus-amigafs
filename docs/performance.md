# Performance baseline

The first release supports amd64 only. Its repeatable baseline uses a generated
20 MiB hard-disc image with one international FFS partition, a 32-entry volume
root, a 16-level path, sixteen cached 1 KiB files and one 4 MiB file read in
64 KiB ranges. Creating the fixture is excluded from every measurement. The
open measurement includes reading the partition table, opening the volume and
indexing the whole tree. The workload does not purge the host page cache, so
it measures application and index costs and warm read paths, not cold-disc
I/O. The memory stress allocates the index for the supported 100,000-entry
ceiling together with one 8 MiB open write buffer.

```shell
make benchmark
```

The command writes `build/performance/amd64.json` and fails if a budget is
missed. Reports include the source revision, platform, fixture definition,
sample summaries and each applied budget.

## First-release amd64 budgets

| Workload | Budget |
| --- | ---: |
| Open and index the image | p95 at most 1,000 ms |
| List the 32-entry root from the index | p95 at most 250 µs |
| Resolve the 16-level path | p95 at most 500 µs |
| Read a warm cached 1 KiB file | p95 at most 100 µs |
| Read the 4 MiB file through ranged reads | median at least 15 MiB/s |
| Python allocation peak while opening | at most 32 MiB |
| Python peak for 100,000 indexed entries plus one 8 MiB write buffer | at most 64 MiB |

These are regression guardrails for shared CI runners, not claims about any
particular storage. On the development machine the same workload opened in
about 25 ms and read the large file at about 65 MiB/s.

## What the baseline does not measure

**Writes.** Each change is staged in memory, optionally validated, journalled
and synchronised. The journal costs one extra write and one `fsync` per
operation. A volume of up to 64 MiB is validated before every commit, which
walks the whole directory tree; a larger volume is validated only when it is
opened read-write and when it is unmounted. Copying many small files to a
floppy image is therefore slower per file than copying them to a large
partition.

**Large files.** A written file is held in memory until it is flushed, copied
once to be handed to the engine, and staged again for the transaction. Writing
a 100 MiB file therefore needs roughly three times that in memory while it is
committed. One operation may stage at most 1 GiB.

**Fragmentation.** The engine's OFS/FFS allocator takes the nearest free block
on either side of a file's header, so a newly written file's blocks alternate
around it. Ranged reads index the block list and are not slowed by this on an
image, but a real Amiga will seek more for such a file than for one written by
AmigaDOS.

**Indexing a large disc.** Opening indexes every entry once. Memory grows by
roughly 0.5 KiB per entry, and an image holding more than 100,000 entries is
refused.

**Containers.** Opening a compressed image decompresses all of it. Opening an
HFE, SCP or IPF image runs `gw convert`, which takes between five and fifteen
seconds for one floppy on the development machine, most of it inside
Greaseweazle. An HFE's density is read from its header to avoid a second
conversion. A writable close adds a second conversion.

**Physical media.** Reading a floppy takes about a minute. Writing back is
proportional to the number of cylinders that changed. A physical disc is read
and written through the kernel's block cache at the speed of the adapter.
