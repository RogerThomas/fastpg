//! The global allocator for Rust-side allocations: mimalloc, optionally wrapped in a counting
//! allocator (feature `alloc-stats`) so benchmarks can report Rust-side allocations next to
//! Python's tracemalloc numbers.

use mimalloc::MiMalloc;

#[cfg(not(feature = "alloc-stats"))]
#[global_allocator]
static GLOBAL: MiMalloc = MiMalloc;

#[cfg(feature = "alloc-stats")]
mod imp {
    use super::MiMalloc;
    use std::alloc::{GlobalAlloc, Layout};
    use std::sync::atomic::{AtomicU64, Ordering};

    static ALLOCATIONS: AtomicU64 = AtomicU64::new(0);

    struct Counting;

    // SAFETY: delegates directly to mimalloc.
    unsafe impl GlobalAlloc for Counting {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            ALLOCATIONS.fetch_add(1, Ordering::Relaxed);
            // SAFETY: the caller upholds `GlobalAlloc::alloc`'s contract, passed on unchanged.
            unsafe { MiMalloc.alloc(layout) }
        }

        unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
            // SAFETY: `ptr` was allocated with `layout` by this allocator, i.e. by `MiMalloc`
            // (`GlobalAlloc::dealloc`'s contract, upheld by the caller).
            unsafe { MiMalloc.dealloc(ptr, layout) }
        }

        unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
            ALLOCATIONS.fetch_add(1, Ordering::Relaxed);
            // SAFETY: `ptr` was allocated with `layout` by this allocator, i.e. by `MiMalloc`,
            // and `new_size` is valid for it (`GlobalAlloc::realloc`'s contract, upheld by the
            // caller).
            unsafe { MiMalloc.realloc(ptr, layout, new_size) }
        }
    }

    #[global_allocator]
    static GLOBAL: Counting = Counting;

    pub fn count() -> Option<u64> {
        Some(ALLOCATIONS.load(Ordering::Relaxed))
    }
}

#[cfg(not(feature = "alloc-stats"))]
mod imp {
    pub fn count() -> Option<u64> {
        None
    }
}

pub use imp::count;
