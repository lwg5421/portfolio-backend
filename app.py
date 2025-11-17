# app.py
import os
import json
import logging
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv
from lxml import etree  # lxml 라이브러리

# ----------------------------
# 로깅 설정
# ----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------
# .env 파일 로드
# ----------------------------
load_dotenv()

app = Flask(__name__)

# === 환경변수 로드 및 검증 ===
DART_API_KEY = os.getenv('DART_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
# [수정됨] 님이 제안하신 2.5 flash 모델의 정식 풀네임으로 변경
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash-preview-09-2025') 
if not DART_API_KEY or not GEMINI_API_KEY:
    logger.error('환경변수 DART_API_KEY 또는 GEMINI_API_KEY가 설정되지 않았습니다.')
    raise RuntimeError('DART_API_KEY와 GEMINI_API_KEY 환경변수가 필요합니다.')

# === CORS 설정 ===
env_origins = os.getenv("ALLOWED_ORIGINS")
ALLOWED_ORIGINS = (
    [o.strip() for o in env_origins.split(",")] if env_origins
    else ['http://localhost:5173', 'http://127.0.0.1:5173', 'http://localhost:5500', 'http://1.2.3.4'] # 1.2.3.4는 임시
)
# Render에서 설정한 환경변수를 사용하도록 수정
CORS(
    app,
    resources={r"/api/*": {"origins": ALLOWED_ORIGINS}},
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=86400
)

# === 안정적인 HTTP 요청을 위한 requests 세션 설정 ===
session = requests.Session()
session.headers.update({'User-Agent': 'portfolio-backend/1.0'})
retries = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))
session.mount('http://', HTTPAdapter(max_retries=retries))

# === CORPCODE.xml 로드 및 준비 ===
CORP_XML_PATH = 'CORPCODE.xml'
corp_name_map = {} # 기업명 -> 기업코드로 빠르게 찾기 위한 딕셔너리(지도)

try:
    if os.path.exists(CORP_XML_PATH):
        logger.info(f"{CORP_XML_PATH} 파일 로드를 시작합니다...")
        context = etree.iterparse(CORP_XML_PATH, events=('end',), tag='list')
        for event, elem in context:
            corp_name = elem.findtext('corp_name')
            corp_code = elem.findtext('corp_code')
            if corp_name and corp_code:
                clean_name = corp_name.replace('(주)', '').strip()
                corp_name_map[clean_name] = {
                    "code": corp_code,
                    "original_name": corp_name
                }
            elem.clear() 
        del context
        logger.info(f"성공: {len(corp_name_map)}개의 기업 정보를 로드했습니다.")
    else:
        logger.warning(f"{CORP_XML_PATH} 파일이 없습니다. /api/search가 작동하지 않습니다.")
        logger.warning(f"Render의 Build Command에 'curl -L [XML링크] -o CORPCODE.xml'가 있는지 확인하세요.")
except Exception as e:
    logger.error(f"{CORP_XML_PATH} 로드 중 오류 발생: {e}", exc_info=True)


# === API 엔드포인트 ===
DART_API_URL = 'https://opendart.fss.or.kr/api'
# [수정됨] v1beta 주소로 복구 (고급 기능을 쓰려면 v1beta가 맞습니다)
GEMINI_URL_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'


def dart_get(path: str, params: dict, timeout: int = 10):
    """DART API GET 요청 헬퍼 함수"""
    params = {'crtfc_key': DART_API_KEY, **params}
    url = f"{DART_API_URL}/{path}"
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


# === 기업명으로 코드 검색 API ===
@app.get('/api/search')
def search_company_code():
    """기업명으로 기업 코드 검색"""
    company_name = request.args.get('name')
    if not company_name:
        return jsonify({'status': '400', 'message': '검색할 기업명(name)이 필요합니다.'}), 400

    if not corp_name_map:
         return jsonify({'status': '500', 'message': '서버에 기업 목록(XML)이 로드되지 않았습니다.'}), 500

    clean_query = company_name.replace('(주)', '').strip()
    result = corp_name_map.get(clean_query)
    
    if result:
        logger.info(f"검색 성공: '{company_name}' -> '{result['code']}'")
        return jsonify({
            'status': '000',
            'corp_code': result['code'],
            'corp_name': result['original_name']
        })
    else:
        logger.warning(f"검색 실패: '{company_name}'에 해당하는 기업을 찾을 수 없습니다.")
        return jsonify({'status': '404', 'message': '일치하는 기업을 찾을 수 없습니다.'}), 404


@app.get('/api/company')
def get_company_overview():
    """기업 개요 정보 조회"""
    corp_code = request.args.get('code')
    if not corp_code:
        return jsonify({'status': '400', 'message': '기업 코드가 필요합니다.'}), 400
    try:
        data = dart_get('company.json', {'corp_code': corp_code})
        return jsonify(data)
    except requests.RequestException as e:
        logger.exception('DART 기업 개요 요청 실패')
        return jsonify({'status': '500', 'message': f'DART API 요청에 실패했습니다: {e}'}), 500


@app.get('/api/finance')
def get_company_finance():
    """기업 재무 정보 조회 (연결재무제표 우선, 없을 시 별도재무제표로 대체)"""
    corp_code = request.args.get('code')
    year = request.args.get('year')
    reprt_code = request.args.get('reprt_code', '11014')  # 사업보고서(연간)
    
    if not corp_code or not year:
        return jsonify({'status': '400', 'message': '기업 코드와 사업 연도가 필요합니다.'}), 400
    try:
        params = {'corp_code': corp_code, 'bsns_year': year, 'reprt_code': reprt_code, 'fs_div': 'CFS'}
        data = dart_get('fnlttSinglAcntAll.json', params)
        
        if data.get('status') != '000' or not data.get('list'):
            logger.info(f"{corp_code}의 연결재무제표가 없어 별도재무제표를 조회합니다.")
            params['fs_div'] = 'OFS'
            data = dart_get('fnlttSinglAcntAll.json', params)
            
        return jsonify(data)
    except requests.RequestException as e:
        logger.exception('DART 재무 정보 요청 실패')
        return jsonify({'status': '500', 'message': f'DART API 요청에 실패했습니다: {e}'}), 500


def extract_first_json(text: str) -> str:
    """문자열에서 괄호 균형이 맞는 첫 번째 JSON 객체만 추출"""
    if not text: return ''
    start = text.find('{')
    if start == -1: return ''
    depth, in_string, escape = 0, False, False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escape: escape = False
            elif char == '\\': escape = True
            elif char == '"': in_string = False
        else:
            if char == '"': in_string = True
            elif char == '{': depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0: return text[start:i+1]
    return ''


def collect_all_texts(gemini_obj) -> str:
    """Gemini API 응답에서 모든 텍스트 조각을 모아 하나의 문자열로 합침"""
    texts = []
    candidates = gemini_obj.get("candidates", []) or []
    for cand in candidates:
        parts = cand.get("content", {}).get("parts", []) or []
        for p in parts:
            text = p.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text)
    return "\n".join(texts).strip()


# [수정됨] v1beta API 스펙에 맞는 camelCase 문법으로 복구
def call_gemini(prompt: str, model: str = None, timeout: int = 60):
    """Gemini API 호출 (v1beta generateContent)"""
    model = model or GEMINI_MODEL
    url = f"{GEMINI_URL_BASE}/{model}:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": "You are a machine that only returns pure JSON."}]},
            {"role": "model", "parts": [{"text": "OK. I will only output a single valid JSON object without any other text or markdown."}]},
            {"role": "user", "parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json", # camelCase
        },
        "systemInstruction": { # camelCase
            "parts": [{
                "text": "You are a helpful assistant that generates company analysis data in JSON format. All textual content in the JSON values MUST be written in Korean."
            }]
        }
    }
    
    response = session.post(url, json=payload, timeout=timeout)
    return response


@app.post('/api/generate-analysis')
def generate_qualitative_analysis():
    """DART 정보 기반으로 Gemini를 호출하여 정성적 기업 분석 데이터 생성"""
    body = request.get_json(silent=True) or {}
    company_name = (body.get('name') or '').strip()
    biz_area = (body.get('bizArea') or '').strip()
    dart_data_str = json.dumps(body.get('dartData', {}), ensure_ascii=False, indent=2)

    if not company_name:
        return jsonify({'error': '회사명 정보가 필요합니다.'}), 400

    schema = """
{
  "vision": "기업의 비전과 목표",
  "productsAndServices": ["주요 제품 및 서비스 1", "주요 제품 및 서비스 2"],
  "performanceSummary": "최근 실적 및 재무 상태 요약",
  "swot": {
    "strength": ["강점 1", "강점 2"],
    "weakness": ["약점 1", "약점 2"],
    "opportunity": ["기회 1", "기회 2"],
    "threat": ["위협 1", "위협 2"],
    "strategy": "SWOT 분석 기반의 추천 전략"
  },
  "industryAnalysis": {
      "method": "산업 분석에 사용한 방법론 (예: 5 Forces Model)",
      "result": "산업의 매력도 및 성장 가능성 분석 결과",
      "competitors": "주요 경쟁사 목록",
      "competitorAnalysis": "경쟁사 대비 강점 및 약점 분석"
  },
  "job": {
    "duties": "프론트엔드 개발자로서의 주요 직무 내용",
    "description": "이 회사에서 프론트엔드 개발자의 역할과 중요성",
    "knowledge": "필요한 기술 지식 (예: React, TypeScript)",
    "skills": "필요한 소프트 스킬 (예: 협업, 문제 해결 능력)",
    "attitude": "요구되는 업무 태도 (예: 성장 지향, 주도성)",
    "certs": "우대 자격증 (없으면 '해당 없음')",
    "env": "개발 환경 및 문화 예측",
    "careerDev": "입사 후 커리어 발전 경로 제안"
  },
  "selfAnalysis": {
      "knowledge": "지원자(나)의 관련 기술 지식 수준 분석",
      "skills": "지원자(나)의 관련 소프트 스킬 수준 분석",
      "attitude": "지원자(나)의 업무 태도 부합도 분석",
      "actionPlan1": "부족한 점 보완을 위한 구체적인 실행 계획 1",
      "actionPlan2": "부족한 점 보완을 위한 구체적인 실행 계획 2",
      "actionPlan3": "부족한 점 보완을 위한 구체적인 실행 계획 3"
  }
}
""".strip()
    
    prompt = (
        "당신은 DART 공시 정보를 기반으로 기업을 심층 분석하는 AI 애널리스트입니다.\n"
        f"분석 대상 기업은 '{company_name}({biz_area})'이며, 지원 직무는 '프론트엔드 개발자'입니다.\n"
        "제공된 DART 데이터와 당신의 지식을 종합하여 아래 JSON 스키마에 맞춰 기업 분석 보고서를 작성해주세요.\n"
        "--- DART 데이터 ---\n"
        f"{dart_data_str}\n"
        "--- JSON 스키마 ---\n"
        f"{schema}\n"
        "--- 중요 규칙 ---\n"
        "1. 모든 텍스트 값은 반드시 '한국어'로 작성해야 합니다. (VERY IMPORTANT: All text values MUST be in Korean.)\n"
        "2. 응답은 다른 설명 없이 순수한 JSON 객체 하나여야 하며, '{'로 시작해서 '}'로 끝나야 합니다."
    )

    try:
        # === 1차 호출 ===
        response = call_gemini(prompt)
        if response.status_code >= 400:
            return jsonify({
                "error": "Gemini API 오류", "status": response.status_code, "upstream": response.text
            }), 502

        api_result_obj = response.json()
        text_content = collect_all_texts(api_result_obj)
        
        # JSON 파싱 시도
        try:
            if text_content:
                json_part = extract_first_json(text_content)
                if json_part:
                    return jsonify(json.loads(json_part))
        except json.JSONDecodeError as e:
            logger.warning(f"1차 Gemini 응답 JSON 파싱 실패: {e}\n원본: {text_content[:500]}")

        # === 2차 복구 호출 ===
        logger.info("1차 분석 실패, JSON 형식 복구를 위한 2차 호출을 시도합니다.")
        repair_prompt = (
            "이전 API 응답이 유효한 JSON이 아닙니다. 아래 원본 텍스트를 분석하여 주어진 JSON 스키마에 맞는 '순수한 JSON 객체'로 복구해주세요.\n"
            "--- 복구할 원본 텍스트 ---\n"
            f"{text_content}\n"
            "--- JSON 스키마 ---\n"
            f"{schema}\n"
            "--- 절대 규칙 ---\n"
            "1. 모든 텍스트 값은 '한국어'로 번역하거나 작성해야 합니다. (ABSOLUTELY MANDATORY: All values must be in Korean.)\n"
            "2. 설명, 주석, 마크다운 등 다른 어떤 텍스트도 없이, 오직 '{'로 시작해서 '}'로 끝나는 JSON 객체 하나만 출력해야 합니다."
        )
        
        response2 = call_gemini(repair_prompt)
        if response2.status_code >= 400:
            return jsonify({
                "error": "Gemini API 오류(복구 시도)", "status": response2.status_code, "upstream": response2.text
            }), 502

        api_result_obj2 = response2.json()
        text_content2 = collect_all_texts(api_result_obj2)

        try:
            if text_content2:
                json_part2 = extract_first_json(text_content2)
                if json_part2:
                    return jsonify(json.loads(json_part2))
        except json.JSONDecodeError as e:
             logger.error(f"2차 복구 시도도 JSON 파싱에 실패했습니다: {e}\n원본: {text_content2[:500]}")
        
        # 최종 실패
        logger.error("Gemini 응답 원문(1차): %s", json.dumps(api_result_obj, ensure_ascii=False)[:1500])
        logger.error("Gemini 응답 원문(2차): %s", json.dumps(api_result_obj2, ensure_ascii=False)[:1500])
        return jsonify({'error': 'Gemini 분석 데이터 생성에 최종 실패했습니다. 응답에서 유효한 JSON을 찾을 수 없습니다.'}), 500

    except requests.RequestException as e:
        logger.exception('Gemini API 요청 중 네트워크 오류 발생')
        return jsonify({'error': 'Gemini API 요청 실패', 'detail': str(e)}), 502
    except Exception as e:
        logger.exception('Gemini 응답 처리 중 서버 내부 오류 발생')
        return jsonify({'error': f'분석 데이터 생성 중 서버 오류 발생: {e}'}), 500


if __name__ == '__main__':
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=False)
