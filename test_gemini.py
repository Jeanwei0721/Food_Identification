import json
import os
import re
from difflib import get_close_matches
from dotenv import load_dotenv
from google import genai
from google.genai import types
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
        default_weight = sum(
            [parse_val(row[col]) for col in weight_cols if col in row]
        )

        db[dish_name] = {
            "tfda_name": str(
                row.get("D. 台灣食藥署(TFDA)資料庫替代品名稱", "")
            ),
            "calories_100g": parse_val(row.get("熱量(100g)")),
            "protein_100g": parse_val(row.get("粗蛋白(100g)")),
            "fat_100g": parse_val(row.get("粗脂肪(100g)")),
            "carbs_100g": parse_val(row.get("總碳水(100g)")),
            "default_weight_g": (
                default_weight if default_weight > 0 else 100.0
            ),
        }
    return db

def analyze_plate_image(image_path, api_key):
    client = genai.Client(api_key=api_key)
    image = Image.open(image_path)

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

    # 確保這裡使用的是 gemini-3.8-flash
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[image, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        ),
    )

    return json.loads(response.text)


def process_and_draw(
    image_path, ai_result, db, output_path="output_analyzed.jpg"
):
    """結合 Excel 比對營養，並繪製邊框與中文字體到圖片上"""
    image = Image.open(image_path)
    width, height = image.size
    draw = ImageDraw.Draw(image)

    font_size = max(18, int(width * 0.02))
    font = None

    font_paths = [
        "msjh.ttc",
        "msjh.ttf",
        "simhei.ttf",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "NotoSansTC-Regular.otf",
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

        left = (xmin / 1000.0) * width
        top = (ymin / 1000.0) * height
        right = (xmax / 1000.0) * width
        bottom = (ymax / 1000.0) * height

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

        draw.rectangle([left, top, right, bottom], outline="red", width=4)

        bbox = font.getbbox(label_text)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1] + 6

        bg_top = top - text_height if top - text_height > 0 else top
        bg_bottom = bg_top + text_height

        draw.rectangle([left, bg_top, left + text_width, bg_bottom], fill="red")
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
    # 改為讀取 GEMINI_API_KEY
    API_KEY = os.getenv("GEMINI_API_KEY")
    EXCEL_PATH = "台鋼餐飲資料20260707.xlsx"
    IMAGE_PATH = "008_1.jpg"

    if not API_KEY:
        print("錯誤：未找到 GEMINI_API_KEY，請確認 .env 檔案設定。")
        exit(1)

    print("1. 載入 Excel 營養資料庫...")
    nutrition_db = load_nutrition_db(EXCEL_PATH)

    print("2. 呼叫 Google 官方 Gemini API (gemini-2.5-flash)...")
    ai_output = analyze_plate_image(IMAGE_PATH, API_KEY)

    print("3. 進行座標轉換、熱量計算與圖片標註...")
    report = process_and_draw(IMAGE_PATH, ai_output, nutrition_db)

    print("\n=== 分析結果 JSON ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n完成！請查看產生的標註圖片：output_analyzed.jpg")