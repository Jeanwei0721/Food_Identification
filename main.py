import base64
import json
import os
import re
from difflib import get_close_matches
from dotenv import load_dotenv
import pandas as pd
from PIL import Image, ImageDraw, ImageFont

# 載入 .env 檔案中的環境變數
load_dotenv()


def load_nutrition_db(excel_path):
    """讀取 Excel 營養資料庫並清洗格式"""
    if not os.path.exists(excel_path):
        raise FileNotFoundError(f"找不到 Excel 檔案：{excel_path}")

    df = pd.read_excel(excel_path, sheet_name="0702菜單資料")
    df_dishes = df.dropna(subset=["B.實際菜名"]).copy()

    db = {}
    for _, row in df_dishes.iterrows():
        dish_name = str(row["B.實際菜名"]).strip()

        def parse_val(val):
            if pd.isna(val):
                return 0.0
            num = re.findall(r"[-+]?\d*\.\d+|\d+", str(val))
            return float(num[0]) if num else 0.0

        weight_cols = [
            "F.主食重(g)",
            "G.主菜1重(g)",
            "H.主菜2重(g)",
            "I.副菜1重(g)",
            "J.副菜2重(g)",
            "K.副菜3重(g)",
            "L.副菜4重(g)",
        ]
        default_weight = sum([parse_val(row[col]) for col in weight_cols if col in row])

        db[dish_name] = {
            "tfda_name": str(row.get("D. 台灣食藥署(TFDA)資料庫替代品名稱", "")),
            "calories_100g": parse_val(row.get("熱量(100g)")),
            "protein_100g": parse_val(row.get("粗蛋白(100g)")),
            "fat_100g": parse_val(row.get("粗脂肪(100g)")),
            "carbs_100g": parse_val(row.get("總碳水(100g)")),
            "default_weight_g": default_weight if default_weight > 0 else 100.0,
        }
    return db


def analyze_plate_image(image_path, api_key):
    """呼叫 OpenRouter VLM 分析圖片邊框與菜名"""
    with open(image_path, "rb") as f:
        base64_image = base64.b64encode(f.read()).decode("utf-8")

    prompt = """
    分析這張餐盤照片中的所有菜色，並回傳 JSON 格式。
    JSON 格式需求：
    {
      "items": [
        {
          "dish_name": "菜色名稱",
          "box_2d": [ymin, xmin, ymax, xmax]  // 範圍 0 到 1000 的整數座標
        }
      ]
    }
    """

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "google/gemini-3.8-flash",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        },
                    },
                ],
            }
        ],
        "response_format": {"type": "json_object"},
    }

    response = requests = __import__("requests").post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
    )
    res_data = response.json()

    if "choices" not in res_data:
        raise Exception(f"API 呼叫失敗：{res_data}")

    return json.loads(res_data["choices"][0]["message"]["content"])


def process_and_draw(
    image_path, ai_result, db, output_path="output_analyzed.jpg"
):
    """結合 Excel 比對營養，並繪製邊框與中文字體到圖片上"""
    image = Image.open(image_path)
    width, height = image.size
    draw = ImageDraw.Draw(image)

    # 嘗試載入支援中文的字型，若找不到則回退到系統預設
    font_size = max(18, int(width * 0.02))  # 依圖片解析度動態調整字體大小
    font = None

    # 常見中文字型路徑（依作業系統自動尋找）
    font_paths = [
        "msjh.ttc",  # Windows 微軟正黑體
        "msjh.ttf",
        "simhei.ttf",  # Windows 黑體
        "/System/Library/Fonts/STHeiti Light.ttc",  # macOS 華康黑體
        "/System/Library/Fonts/PingFang.ttc",  # macOS 蘋方
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",  # Linux 文泉驛正黑
        "NotoSansTC-Regular.otf",  # 思源黑體
    ]

    for path in font_paths:
        try:
            font = ImageFont.truetype(path, font_size)
            break
        except OSError:
            continue

    if font is None:
        print(
            "警告：未找到中文字型檔，中文可能無法正常顯示。請自行指定 .ttf 字型檔路徑。"
        )
        font = ImageFont.load_default()

    total_meal_calories = 0
    analyzed_items = []

    for item in ai_result.get("items", []):
        detected_name = item["dish_name"]
        ymin, xmin, ymax, xmax = item["box_2d"]

        # 座標轉實際像素
        left = (xmin / 1000.0) * width
        top = (ymin / 1000.0) * height
        right = (xmax / 1000.0) * width
        bottom = (ymax / 1000.0) * height

        # 模糊比對菜單
        matches = get_close_matches(
            detected_name, list(db.keys()), n=1, cutoff=0.2
        )
        matched_key = matches[0] if matches else None

        if matched_key:
            nutrition = db[matched_key]
            weight_g = nutrition["default_weight_g"]
            calories = (nutrition["calories_100g"] / 100.0) * weight_g
            label_text = f" {matched_key} (~{weight_g:.0f}g, {calories:.0f}kcal) "
        else:
            weight_g = 0
            calories = 0
            label_text = f" {detected_name} (未比對到) "

        total_meal_calories += calories

        # 畫紅色外框
        draw.rectangle([left, top, right, bottom], outline="red", width=4)

        # 使用 font.getbbox 計算文字精確寬高
        bbox = font.getbbox(label_text)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1] + 6

        # 計算背景框的位置 (若靠頂部則往下畫，避免超出圖片)
        bg_top = top - text_height if top - text_height > 0 else top
        bg_bottom = bg_top + text_height

        # 標籤紅色背景框
        draw.rectangle([left, bg_top, left + text_width, bg_bottom], fill="red")

        # 繪製白色文字
        draw.text((left, bg_top), label_text, fill="white", font=font)

        analyzed_items.append(
            {
                "dish_name": matched_key or detected_name,
                "weight_g": weight_g,
                "calories_kcal": round(calories, 1),
                "box_2d": [ymin, xmin, ymax, xmax],
            }
        )

    image.save(output_path)

    return {
        "total_calories_kcal": round(total_meal_calories, 1),
        "items": analyzed_items,
    }


if __name__ == "__main__":
    API_KEY = os.getenv("OPENROUTER_API_KEY")
    EXCEL_PATH = "台鋼餐飲資料20260707.xlsx"
    IMAGE_PATH = "008_1.jpg"

    if not API_KEY:
        print("錯誤：未找到 OPENROUTER_API_KEY，請確認 .env 檔案設定。")
        exit(1)

    print("1. 載入 Excel 營養資料庫...")
    nutrition_db = load_nutrition_db(EXCEL_PATH)

    print("2. 呼叫 OpenRouter API (gemini-3.8-flash)...")
    ai_output = analyze_plate_image(IMAGE_PATH, API_KEY)

    print("3. 進行座標轉換、熱量計算與圖片標註...")
    report = process_and_draw(IMAGE_PATH, ai_output, nutrition_db)

    print("\n=== 分析結果 JSON ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n完成！請查看產生的標註圖片：output_analyzed.jpg")