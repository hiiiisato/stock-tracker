"""株式分割の互換エントリーポイント。公式台帳と価格公開処理は共通化済み。

旧処理はarchive/splits_legacy_2026_07.pyに保管。
python splits.py --backfill は公式台帳同期後に過去系列を再生成する。
"""
from __future__ import annotations


def ensure_splits_table():
    from price_store import ensure_schema
    ensure_schema()


def rebuild(codes4=None, full=True, use_jquants=True):
    from corporate_actions import sync_official_actions
    from price_pipeline import repair_codes
    result = sync_official_actions(history=full)
    repaired = repair_codes(codes4)
    errors = result['errors'] + list(repaired['errors'].values())
    if errors:
        raise RuntimeError(' / '.join(errors[:12]))
    return sum(repaired['repaired'].values())


def run_daily(weeks=16):
    from price_pipeline import check_integrity, repair_codes
    from price_store import watched_codes
    result = check_integrity()
    if not result['held']:
        return 0
    repaired = repair_codes(result['held'])
    if repaired['errors']:
        print('  分割補正の個別保留（公開対象から除外・次便で再試行）: ' + str(repaired['errors']))
        if watched := watched_codes(repaired['errors']):
            raise RuntimeError('ウォッチリストの価格補正を保留: ' + ','.join(sorted(watched)))
    return len(repaired['repaired'])


def run_integrity_check(days=45):
    from price_pipeline import check_integrity
    result = check_integrity()
    if result['held']:
        raise RuntimeError('分割補正を保留: ' + ','.join(result['held']))
    return 0


if __name__ == '__main__':
    import sys
    if '--backfill' in sys.argv:
        rebuild()
    elif '--daily' in sys.argv:
        run_daily()
    elif '--check' in sys.argv:
        run_integrity_check()
    else:
        print('python splits.py [--backfill | --daily | --check]')
