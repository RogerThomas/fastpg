//! Raw row storage filled by the I/O tasks, decoded into Python objects when an operation
//! resolves.
//!
//! xitca exposes typed access only (`Row::try_get::<T: FromSql>`); there is no public raw
//! accessor. [`RawCell`] is a `FromSql` impl that accepts every type and hands back the
//! untouched binary value, which we copy into one contiguous arena per result. That is one
//! memcpy per cell and amortised O(1) allocations per result, independent of row count.

use std::error::Error;

use xitca_postgres::row::Row;
use xitca_postgres::types::{FromSql, Type};

use crate::errors::QueryError;

struct RawCell<'a>(Option<&'a [u8]>);

impl<'a> FromSql<'a> for RawCell<'a> {
    fn from_sql(_: &Type, raw: &'a [u8]) -> Result<Self, Box<dyn Error + Sync + Send>> {
        Ok(Self(Some(raw)))
    }

    fn from_sql_null(_: &Type) -> Result<Self, Box<dyn Error + Sync + Send>> {
        Ok(Self(None))
    }

    fn accepts(_: &Type) -> bool {
        true
    }
}

const NULL: u32 = u32::MAX;

/// Rows of at most this many columns are read in one pass (see [`RowBuffer::push_row`]).
const ONE_PASS_COLS: usize = 16;

pub struct RowBuffer {
    ncols: usize,
    nrows: usize,
    data: Vec<u8>,
    /// (offset, len) per cell, row-major; `len == NULL` marks SQL NULL.
    cells: Vec<(u32, u32)>,
}

impl RowBuffer {
    pub fn new(ncols: usize) -> Self {
        Self {
            ncols,
            nrows: 0,
            data: Vec::new(),
            cells: Vec::new(),
        }
    }

    /// This buffer emptied for `ncols` columns, keeping its allocations.
    pub fn reset(mut self, ncols: usize) -> Self {
        self.ncols = ncols;
        self.nrows = 0;
        self.data.clear();
        self.cells.clear();
        self
    }

    /// Bytes allocated for the buffer's data and cell index.
    pub fn capacity_bytes(&self) -> usize {
        self.data.capacity() + self.cells.capacity() * std::mem::size_of::<(u32, u32)>()
    }

    pub fn nrows(&self) -> usize {
        self.nrows
    }

    pub fn push_row(&mut self, row: &Row<'_>) -> Result<(), QueryError> {
        // Reserve the row's bytes and cells up front: growing both vectors cell by cell
        // costs several reallocations per row, i.e. per point lookup. Rows of up to
        // `ONE_PASS_COLS` columns keep their cells on the stack while the size is summed,
        // so each cell is looked up once; wider rows look them up again to copy them.
        if self.ncols <= ONE_PASS_COLS {
            let mut values = [None; ONE_PASS_COLS];
            let values = &mut values[..self.ncols];
            let mut bytes = 0;
            for (i, slot) in values.iter_mut().enumerate() {
                let RawCell(value) = row.try_get::<RawCell<'_>>(i)?;
                bytes += value.map_or(0, <[u8]>::len);
                *slot = value;
            }
            self.data.reserve(bytes);
            self.cells.reserve(self.ncols);
            for value in values.iter() {
                self.push_cell(*value)?;
            }
        } else {
            let mut bytes = 0;
            for i in 0..self.ncols {
                if let RawCell(Some(value)) = row.try_get::<RawCell<'_>>(i)? {
                    bytes += value.len();
                }
            }
            self.data.reserve(bytes);
            self.cells.reserve(self.ncols);
            for i in 0..self.ncols {
                let RawCell(value) = row.try_get::<RawCell<'_>>(i)?;
                self.push_cell(value)?;
            }
        }
        self.nrows += 1;
        Ok(())
    }

    fn push_cell(&mut self, value: Option<&[u8]>) -> Result<(), QueryError> {
        let cell = match value {
            None => (0, NULL),
            Some(bytes) => {
                let start = self.data.len();
                let end = start + bytes.len();
                if end >= NULL as usize {
                    return Err(QueryError::Interface(
                        "result set exceeds 4 GiB; fetch it in smaller batches".into(),
                    ));
                }
                self.data.extend_from_slice(bytes);
                (start as u32, bytes.len() as u32)
            }
        };
        self.cells.push(cell);
        Ok(())
    }

    /// Value of `col` in row `row`, `None` for SQL NULL.
    #[inline]
    pub fn cell(&self, row: usize, col: usize) -> Option<&[u8]> {
        let (start, len) = self.cells[row * self.ncols + col];
        (len != NULL).then(|| &self.data[start as usize..(start + len) as usize])
    }
}
