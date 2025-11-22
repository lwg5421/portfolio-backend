import os
import json
import logging
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv
from lxml import etree
from bs4 import BeautifulSoup 

# ----------------------------
# 1. 기본 설정 및 환경변수
# ----------------------------
load_dotenv() # .env 파일 로드

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# CORS 설정: 모든 출처 허용 (배포 및 로컬 테스트 호환)
CORS(app, resources={r"/*": {"origins": "*"}})

# === API 키 가져오기 ===
DART_API_KEY = os.getenv('DART_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash-preview-09-2025')

# HTTP 세션 설정 (재시도 로직 포함)
session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
})
retries = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))
session.mount('http://', HTTPAdapter(max_retries=retries))

# ----------------------------
# 2. 데이터 로드 (CORPCODE.xml)
# ----------------------------
CORP_XML_PATH = 'CORPCODE.xml'
corp_name_map = {}

try:
    if os.path.exists(CORP_XML_PATH):
        logger.info(f"파일 로드 중: {CORP_XML_PATH}")
        context = etree.iterparse(CORP_XML_PATH, events=('end',), tag='list')
        for event, elem in context:
            c_name = elem.findtext('corp_name')
            c_code = elem.findtext('corp_code')
            if c_name and c_code:
                # (주) 제거 및 공백 제거
                clean_name = c_name.replace('(주)', '').strip()
                corp_name_map[clean_name] = {"code": c_code, "original_name": c_name}
            elem.clear()
        del context
        logger.info(f"기업 정보 로드 완료: {len(corp_name_map)}개")
    else:
        logger.warning("CORPCODE.xml 파일이 없습니다. 기업 검색 기능이 제한됩니다.")
except Exception as e:
    logger.error(f"XML 로드 에러: {e}")

# ----------------------------
# 3. 헬퍼 함수들
# ----------------------------
DART_API_URL = 'https://opendart.fss.or.kr/api'
GEMINI_URL_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'

def dart_get(path, params):
    """DART API 호출"""
    if not DART_API_KEY: return {}
    params['crtfc_key'] = DART_API_KEY
    res = session.get(f"{DART_API_URL}/{path}", params=params, timeout=15)
    res.raise_for_status()
    return res.json()

def call_gemini(prompt):
    """Gemini API 호출"""
    if not GEMINI_API_KEY: return requests.Response()
    url = f"{GEMINI_URL_BASE}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    # JSON 응답 강제 설정
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096, "responseMimeType": "application/json"}
    }
    return session.post(url, json=payload, timeout=60)

def collect_text(gemini_res):
    """Gemini 응답에서 텍스트 추출"""
    texts = []
    for cand in gemini_res.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if part.get("text"): texts.append(part["text"])
    return "\n".join(texts).strip()

def extract_json(text):
    """JSON 부분만 추출"""
    if not text: return ""
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        return text[start:end+1]
    return ""

def fetch_google_news(keyword):
    """구글 뉴스 RSS 가져오기 (안정적)"""
    rss_url = f"https://news.google.com/rss/search?q={keyword}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        response = session.get(rss_url, timeout=5)
        if response.status_code != 200: 
            logger.error(f"구글 뉴스 접속 실패: {response.status_code}")
            return []
        
        # XML 파싱
        soup = BeautifulSoup(response.content, 'xml')
        items = soup.find_all('item')
        
        news_list = []
        for item in items[:5]: # 상위 5개만
            title = item.title.text if item.title else "제목 없음"
            link = item.link.text if item.link else "#"
            pubDate = item.pubDate.text if item.pubDate else ""
            
            # HTML 태그 제거된 설명글 추출
            raw_desc = item.description.text if item.description else ""
            desc_soup = BeautifulSoup(raw_desc, 'html.parser')
            description = desc_soup.get_text(strip=True)

            news_list.append({
                "title": title,
                "description": description[:100] + "...", 
                "link": link,
                "pubDate": pubDate
            })
        logger.info(f"뉴스 수집 성공: {len(news_list)}개")
        return news_list
    except Exception as e:
        logger.error(f"뉴스 가져오기 실패: {e}")
        return []

# ----------------------------
# 4. 라우팅 (웹페이지 + API)
# ----------------------------

# [핵심] 메인 페이지 접속 시 index.html 반환 (404 에러 방지)
@app.route('/')
def home():
    try:
        return send_file('index.html')
    except Exception as e:
        return f"<h3>index.html 파일을 찾을 수 없습니다.</h3><p>app.py와 같은 폴더에 있는지 확인해주세요.<br>에러: {e}</p>"

# 404 에러 핸들러 (HTML 대신 JSON 반환)
@app.errorhandler(404)
def page_not_found(e):
    return jsonify(error="404 Not Found", message="요청하신 API 경로가 잘못되었습니다."), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify(error="500 Server Error", message="서버 내부 오류가 발생했습니다."), 500

# --- API 엔드포인트 ---

@app.route('/api/search', methods=['GET'])
def search():
    name = request.args.get('name', '').strip()
    clean_name = name.replace('(주)', '').strip()
    
    if not name: return jsonify({'status': '400', 'message': '기업명을 입력하세요.'}), 400
    
    res = corp_name_map.get(clean_name)
    if res:
        return jsonify({'status': '000', 'corp_code': res['code'], 'corp_name': res['original_name']})
    
    return jsonify({'status': '404', 'message': '일치하는 기업을 찾을 수 없습니다.'}), 404

@app.route('/api/company', methods=['GET'])
def company():
    code = request.args.get('code')
    try:
        return jsonify(dart_get('company.json', {'corp_code': code}))
    except Exception as e:
        return jsonify({'status': '500', 'message': str(e)}), 500

@app.route('/api/finance', methods=['GET'])
def finance():
    code = request.args.get('code')
    year = request.args.get('year')
    try:
        # 1순위: 연결재무제표
        data = dart_get('fnlttSinglAcntAll.json', {'corp_code': code, 'bsns_year': year, 'reprt_code': '11014', 'fs_div': 'CFS'})
        # 2순위: 별도재무제표
        if data.get('status') != '000' or not data.get('list'):
            data = dart_get('fnlttSinglAcntAll.json', {'corp_code': code, 'bsns_year': year, 'reprt_code': '11014', 'fs_div': 'OFS'})
        return jsonify(data)
    except Exception as e:
        return jsonify({'status': '500', 'message': str(e)}), 500

@app.route('/api/generate-analysis', methods=['POST'])
def analyze():
    data = request.get_json()
    name = data.get('name', '')
    biz = data.get('bizArea', '')
    
    schema = """{"vision": "비전(한글)", "productsAndServices": ["제품1"], "performanceSummary": "실적요약(한글)", "swot": {"strength": [], "weakness": [], "opportunity": [], "threat": [], "strategy": ""}, "industryAnalysis": {"method": "", "result": "", "competitors": "", "competitorAnalysis": ""}, "job": {"duties": "", "description": "", "knowledge": "", "skills": "", "attitude": "", "certs": "", "env": "", "careerDev": ""}, "selfAnalysis": {"knowledge": "", "skills": "", "attitude": "", "actionPlan1": "", "actionPlan2": "", "actionPlan3": ""}}"""
    
    prompt = f"기업 '{name}({biz})'을 프론트엔드 개발자 취업 준비생 관점에서 분석해줘. 아래 JSON 포맷만 리턴해.\n{schema}"
    
    try:
        res = call_gemini(prompt)
        if res.status_code != 200: return jsonify({'error': 'Gemini Error', 'details': res.text}), 500
        
        text = collect_text(res.json())
        json_str = extract_json(text)
        
        if json_str:
            return jsonify(json.loads(json_str))
        else:
            # 2차 복구 시도
            res2 = call_gemini(f"Fix JSON:\n{text}")
            return jsonify(json.loads(extract_json(collect_text(res2.json()))))
            
    except Exception as e:
        logger.error(f"분석 에러: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/news-summary', methods=['POST'])
def news_summary():
    data = request.get_json()
    keyword = data.get('keyword')
    
    logger.info(f"뉴스 요청: {keyword}")

    # 1. 구글 뉴스 RSS 가져오기 (실제 데이터)
    news_items = fetch_google_news(keyword)
    
    # 2. 결과가 없으면 정직하게 반환
    if not news_items:
        return jsonify({
            'news_list': [],
            'ai_summary': f"<b>'{keyword}'에 대한 뉴스 검색 결과가 없습니다.</b><br>검색어를 확인하거나, 기업명을 정확히 입력해주세요."
        })

    # 3. Gemini 요약 (JSON 파싱 처리 강화)
    summary = "요약 실패"
    try:
        news_text = "\n".join([f"{i+1}. {n['title']}" for i, n in enumerate(news_items)])
        
        # JSON으로 달라고 요청
        prompt = (
            f"다음 '{keyword}' 관련 뉴스 제목들을 보고 취업 면접 대비용으로 3줄 핵심 요약해줘.\n"
            "형식: <ul><li>핵심1</li><li>핵심2</li><li>핵심3</li></ul>\n"
            "반환값은 반드시 다음 JSON 포맷이어야 해: {\"summary\": \"HTML문자열\"}\n"
            f"뉴스 목록:\n{news_text}"
        )
        
        res = call_gemini(prompt)
        if res.status_code == 200:
            raw_json_text = collect_text(res.json())
            try:
                # JSON 파싱 시도
                parsed_obj = json.loads(raw_json_text)
                # 'summary' 키만 뽑아내기
                if "summary" in parsed_obj:
                    summary = parsed_obj["summary"]
                else:
                    summary = list(parsed_obj.values())[0]
            except json.JSONDecodeError:
                # 파싱 실패하면 원본 그대로 사용 (혹시 모르니)
                summary = raw_json_text

    except Exception as e:
        logger.error(f"요약 생성 에러: {e}")

    return jsonify({'news_list': news_items, 'ai_summary': summary})

if __name__ == '__main__':
    # Render에서는 PORT 환경변수를 사용
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=True)
