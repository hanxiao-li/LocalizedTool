"""Excel read/write helpers built on pandas + openpyxl."""

import os
import uuid

import pandas as pd
from werkzeug.utils import secure_filename

from config import UPLOAD_DIR


def read_excel(filepath: str, header: int = 0, sheet: str | None = None) -> pd.DataFrame:
    """Read an Excel file as all-strings, empty cells -> ''.

    Falls back to the first sheet when the requested sheet is missing.
    """
    try:
        if sheet:
            return pd.read_excel(filepath, sheet_name=sheet, header=header,
                                 dtype=str).fillna('')
    except (ValueError, KeyError):
        pass
    return pd.read_excel(filepath, header=header, dtype=str).fillna('')


def list_sheets(filepath: str) -> list[str]:
    xls = pd.ExcelFile(filepath)
    return list(xls.sheet_names)


def save_upload(file_storage, subfolder: str = '') -> str:
    """Persist an uploaded file under data/uploads and return its path."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe = secure_filename(file_storage.filename or 'upload.xlsx')
    filename = f'{uuid.uuid4().hex}_{safe}'
    if subfolder:
        folder = os.path.join(UPLOAD_DIR, subfolder)
        os.makedirs(folder, exist_ok=True)
        filename = f'{subfolder}_{filename}'
    path = os.path.join(UPLOAD_DIR, filename)
    file_storage.save(path)
    return path


def df_to_excel_bytes(df: pd.DataFrame, sheet_name: str = 'Sheet1') -> bytes:
    """Serialize a DataFrame to .xlsx bytes for in-memory file serving."""
    import io
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
    buf.seek(0)
    return buf.getvalue()


def write_excel(path: str, df: pd.DataFrame, sheet_name: str = 'Sheet1',
                header: bool = True, index: bool = False) -> None:
    """Write a DataFrame to disk (creates parent dirs)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with pd.ExcelWriter(path, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=index, header=header)


def columns_of(filepath: str) -> list[str]:
    """Column names of the first sheet (headers assumed on row 0)."""
    df = read_excel(filepath, header=0)
    return [str(c) for c in df.columns]


def str_rows(filepath: str, column: str, max_rows: int | None = None) -> list[str]:
    """Non-empty string values of a column, deduplicated, order preserved."""
    df = read_excel(filepath, header=0)
    if column not in df.columns:
        return []
    seen = []
    for v in df[column].dropna():
        s = str(v).strip()
        if s and s != 'nan' and s not in seen:
            seen.append(s)
        if max_rows and len(seen) >= max_rows:
            break
    return seen
