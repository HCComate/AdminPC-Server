import csv
import os


def load_error_codes(csv_path=None):
    """
    error_code.csv를 읽어 에러 코드 딕셔너리로 반환

    반환 형식:
    {
        "HM-PO-01": {
            "code": "HM-PO-01",
            "category": "하드웨어 · 기계",
            "name": "비상 정지 버튼 활성",
            "situation": "작업자 또는 안전 센서가 비상 정지 트리거",
            "severity": "Critical",
            "action": "즉시 라인 중단, 원인 파악 후 재가동 승인"
        },
        ...
    }
    """
    if csv_path is None:
        # 이 파일과 같은 폴더의 error_code.csv를 기본으로 사용
        csv_path = os.path.join(os.path.dirname(__file__), 'error_code.csv')

    error_dict = {}

    with open(csv_path, mode='r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)

        # 처음 3줄은 제목/설명 줄이므로 건너뜀
        for _ in range(3):
            next(reader)

        # 4번째 줄이 실제 헤더
        headers = next(reader)
        # ['오류코드', '대분류', '오류명', '발생 상황', '심각도', '대응 방법', '구 코드 참조']

        for row in reader:
            if not row or not row[0].strip():
                continue  # 빈 줄 건너뜀

            code = row[0].strip()
            error_dict[code] = {
                "code": code,
                "category": row[1].strip() if len(row) > 1 else "",
                "name": row[2].strip() if len(row) > 2 else "",
                "situation": row[3].strip() if len(row) > 3 else "",
                "severity": row[4].strip().upper() if len(row) > 4 else "MEDIUM",
                "action": row[5].strip() if len(row) > 5 else "",
            }

    return error_dict


def filter_equipment_errors(error_dict):
    """공정 오류만 필터링 (HM, HV, HS, SV, SS 접두어)"""
    equipment_prefixes = ('HM', 'HV', 'HS', 'SV', 'SS')
    return {
        code: info for code, info in error_dict.items()
        if code[:2] in equipment_prefixes
    }


def filter_app_errors(error_dict):
    """앱 오류만 필터링 (AM, AN, AD, AS, AG, AX, AH 접두어)"""
    app_prefixes = ('AM', 'AN', 'AD', 'AS', 'AG', 'AX', 'AH')
    return {
        code: info for code, info in error_dict.items()
        if code[:2] in app_prefixes
    }


def filter_by_severity(error_dict, severity):
    """특정 심각도만 필터링"""
    return {
        code: info for code, info in error_dict.items()
        if info["severity"] == severity.upper()
    }
