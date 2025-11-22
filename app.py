import os
import json
import logging
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from requests.adapters import HTTPAdapter, Retry
from dotenv import load_dotenv
from lxml import etree
from bs4 import BeautifulSoup # [필수] 크롤링

# ----------------------------
# 1. 기본 설정
# ----------------------------
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# CORS 설정
CORS(app, resources={r"/api/*": {"origins": "*"}})

# === API 키 ===
DART_API_KEY = os.getenv('DART_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash-preview-09-2025')

# HTTP 세션
session = requests.Session()
# [중요] 로봇이 아닌 척하기 위해 User-Agent를 최신 크롬 브라우저처럼 설정
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7'
})
retries = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))
session.mount('http://', HTTPAdapter(max_retries=retries))

# ----------------------------
# 2. 데이터 로드 (XML)
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
                clean_name = c_name.replace('(주)', '').strip()
                corp_name_map[clean_name] = {"code": c_code, "original_name": c_name}
            elem.clear()
        del context
        logger.info(f"기업 정보 로드 완료: {len(corp_name_map)}개")
    else:
        logger.warning("CORPCODE.xml 파일이 없습니다.")
except Exception as e:
    logger.error(f"XML 로드 에러: {e}")

# ----------------------------
# 3. 헬퍼 함수들
# ----------------------------
DART_API_URL = 'https://opendart.fss.or.kr/api'
GEMINI_URL_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'

def dart_get(path, params):
    if not DART_API_KEY: return {}
    params['crtfc_key'] = DART_API_KEY
    res = session.get(f"{DART_API_URL}/{path}", params=params, timeout=15)
    res.raise_for_status()
    return res.json()

def call_gemini(prompt):
    if not GEMINI_API_KEY: return requests.Response()
    url = f"{GEMINI_URL_BASE}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096, "responseMimeType": "application/json"}
    }
    return session.post(url, json=payload, timeout=60)

def collect_text(gemini_res):
    texts = []
    for cand in gemini_res.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if part.get("text"): texts.append(part["text"])
    return "\n".join(texts).strip()

def extract_json(text):
    if not text: return ""
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        return text[start:end+1]
    return ""

# [핵심] 강력해진 크롤링 함수
def crawl_naver_news(keyword):
    base_url = "https://search.naver.com/search.naver"
    params = {
        "where": "news",
        "query": keyword,
        "sort": "0", # 관련도순
    }
    
    try:
        response = session.get(base_url, params=params, timeout=5)
        # 응답이 성공했는지 확인
        if response.status_code != 200:
            logger.error(f"네이버 접속 실패: {response.status_code}")
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        news_list = []
        
        # [수정] 더 확실한 태그(뉴스 제목 클래스)를 직접 찾음
        # 'news_tit' 클래스는 네이버 뉴스 제목 링크의 고유 클래스입니다.
        title_tags = soup.select("a.news_tit")
        
        if not title_tags:
            logger.warning(f"'{keyword}' 검색 결과에서 뉴스 태그(news_tit)를 찾을 수 없습니다. HTML 구조가 다르거나 차단되었을 수 있습니다.")
            # 디버깅용: HTML 앞부분 조금만 로그에 찍어봄
            # logger.info(response.text[:500]) 
            return []

        for tag in title_tags[:5]: # 상위 5개
            title = tag.get_text()
            link = tag['href']
            desc = "내용 요약 없음"
            
            # 설명글(dsc) 찾기: 제목 태그의 부모 영역 근처에 있음
            # 보통 a.news_tit 옆이나 부모의 형제 요소에 div.news_dsc가 있음
            parent_area = tag.find_parent('div', class_='news_area')
            if parent_area:
                dsc_tag = parent_area.select_one("div.news_dsc")
                if dsc_tag:
                    desc = dsc_tag.get_text(strip=True)
            
            news_list.append({
                "title": title,
                "description": desc,
                "link": link,
                "pubDate": "최근"
            })
            
        logger.info(f"크롤링 성공: {len(news_list)}개 수집됨")
        return news_list

    except Exception as e:
        logger.error(f"크롤링 중 에러 발생: {e}")
        return []

# ----------------------------
# 4. 웹페이지 서빙
# ----------------------------
@app.route('/')
def home():
    try:
        return send_file('index.html')
    except Exception as e:
        return f"index.html 파일이 없습니다. {e}"

# ----------------------------
# 5. API 엔드포인트
# ----------------------------
@app.route('/api/search', methods=['GET'])
def search():
    name = request.args.get('name', '').strip()
    clean_name = name.replace('(주)', '').strip()
    if not name: return jsonify({'status': '400', 'message': '기업명을 입력하세요.'}), 400
    
    res = corp_name_map.get(clean_name)
    if res:
        return jsonify({'status': '000', 'corp_code': res['code'], 'corp_name': res['original_name']})
    return jsonify({'status': '404', 'message': '기업을 찾을 수 없습니다.'}), 404

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
        data = dart_get('fnlttSinglAcntAll.json', {'corp_code': code, 'bsns_year': year, 'reprt_code': '11014', 'fs_div': 'CFS'})
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
    
    schema = """
    {
      "vision": "기업 비전(한국어)",
      "productsAndServices": ["제품1", "제품2"],
      "performanceSummary": "실적 요약(한국어)",
      "swot": {"strength": [], "weakness": [], "opportunity": [], "threat": [], "strategy": "전략(한국어)"},
      "industryAnalysis": {"method": "", "result": "", "competitors": "", "competitorAnalysis": ""},
      "job": {"duties": "", "description": "", "knowledge": "", "skills": "", "attitude": "", "certs": "", "env": "", "careerDev": ""},
      "selfAnalysis": {"knowledge": "", "skills": "", "attitude": "", "actionPlan1": "", "actionPlan2": "", "actionPlan3": ""}
    }
    """
    prompt = f"기업 '{name}({biz})'을 프론트엔드 개발자 취업 준비생 관점에서 분석해줘. 아래 JSON 형식으로만 답해줘.\n{schema}"
    
    try:
        res = call_gemini(prompt)
        if res.status_code != 200: return jsonify({'error': 'Gemini Error', 'details': res.text}), 500
        
        text = collect_text(res.json())
        json_str = extract_json(text)
        if json_str:
            return jsonify(json.loads(json_str))
        else:
            res2 = call_gemini(f"Fix JSON:\n{text}")
            return jsonify(json.loads(extract_json(collect_text(res2.json()))))
    except Exception as e:
        logger.error(f"분석 에러: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/news-summary', methods=['POST'])
def news_summary():
    data = request.get_json()
    keyword = data.get('keyword')
    
    logger.info(f"뉴스 크롤링 요청: {keyword}")

    # 1. 크롤링
    news_items = crawl_naver_news(keyword)

    # 2. 결과 없음 처리
    if not news_items:
        return jsonify({
            'news_list': [],
            'ai_summary': f"<b>'{keyword}'에 대한 뉴스 검색 결과가 없습니다.</b><br>네이버 검색 페이지 구조가 변경되었거나, 서버 접근이 차단되었을 수 있습니다."
        })

    # 3. Gemini 요약
    summary = "요약 실패"
    try:
        news_text = "\n".join([f"{i+1}. {n['title']}: {n['description']}" for i, n in enumerate(news_items)])
        prompt = f"다음 '{keyword}' 관련 뉴스들을 취업 면접 대비용으로 3줄로 핵심 요약해줘. HTML 태그(<ul>, <li>, <b>)를 사용해서 출력해:\n{news_text}"
        
        res = call_gemini(prompt)
        if res.status_code == 200:
            summary = collect_text(res.json()).replace('```html', '').replace('```', '').strip()
    except Exception as e:
        logger.error(f"요약 생성 에러: {e}")

    return jsonify({'news_list': news_items, 'ai_summary': summary})

if __name__ == '__main__':
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=True)
