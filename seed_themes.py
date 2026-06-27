#!/usr/bin/env python3
"""
テーママスタ・銘柄×テーマ関連の初期データ投入スクリプト。
データソース: 株探テーマページ + 公知の事業内容に基づき精査・スコアリング。

relevance:  3=コア（主力事業）/ 2=関連（主要事業の一部）/ 1=周辺（間接的）

実行:
  python seed_themes.py          # INSERT / UPDATE（新規追加・変更のみ）
  python seed_themes.py --sync   # マッピングを完全同期（削除も反映）
  python seed_themes.py --check  # DBと突合して未登録コードを表示
"""
from __future__ import annotations
import sys
from config import get_conn, bulk_upsert

# ─────────────────────────────────────────
# 1. テーマ階層定義
# ─────────────────────────────────────────
# (code, name, name_en, parent_code, level, description, sort_order)
CATEGORIES = [
    # ── 大分類 ──────────────────────────────
    ("TECH",     "テクノロジー",       "Technology",            None, 1, "AI・半導体・クラウド・セキュリティ・フィジカルAI",  10),
    ("DEFENSE",  "防衛・安全保障",     "Defense & Security",    None, 1, "防衛装備・宇宙・ドローン",                          20),
    ("ENERGY",   "エネルギー・環境",   "Energy & Environment",  None, 1, "再エネ・電池・水素・脱炭素",                        30),
    ("CONSUMER", "消費・サービス",     "Consumer & Services",   None, 1, "インバウンド・医療DX",                              40),
    ("MOBILITY", "製造・モビリティ",   "Manufacturing",         None, 1, "EV・次世代車",                                      50),

    # ── 小分類: TECH ────────────────────────
    ("AI_GEN",   "生成AI・LLM",        "Generative AI",         "TECH",    2, "生成AIサービス・LLM開発・AI受託",               11),
    ("AI_INFRA", "AIインフラ・DC",      "AI Infrastructure",     "TECH",    2, "データセンター建設・GPU・冷却設備",             12),
    ("SEMI",     "半導体",             "Semiconductor",         "TECH",    2, "製造装置・ウェーハ・材料・メモリ・マイコン",     13),
    ("CYBER",    "サイバーセキュリティ","Cybersecurity",         "TECH",    2, "EDR・ゼロトラスト・脆弱性管理",                 14),
    ("ROBOT",    "ロボット・FA",        "Robotics & Factory",    "TECH",    2, "産業ロボット・精密減速機・FA制御",              15),
    ("CLOUD",    "クラウド・SaaS",      "Cloud & SaaS",          "TECH",    2, "クラウド基盤・SaaS・DXサービス",                16),
    # フィジカルAI: AI×物理世界（ロボット・産業機械・ARM/組み込みAI）
    ("PHYS_AI",  "フィジカルAI",        "Physical AI",           "TECH",    2, "産業AIロボット・工場自動化AI・組み込みAI",      17),

    # ── 小分類: DEFENSE ─────────────────────
    ("DEF_EQ",   "防衛装備",           "Defense Equipment",     "DEFENSE", 2, "艦艇・航空機・ミサイル・火工品",                21),
    # 宇宙テーマ: 衛星・ロケット・宇宙インフラ（ドローンは DRONE へ独立）
    ("SPACE",    "宇宙",               "Space",                 "DEFENSE", 2, "ロケット・人工衛星・宇宙通信・月面探査",         22),
    # ドローンテーマ: 産業・防衛ドローン（宇宙とは明確に分離）
    ("DRONE",    "ドローン",           "Drone",                 "DEFENSE", 2, "産業ドローン・防衛ドローン・自律飛行",           23),

    # ── 小分類: ENERGY ──────────────────────
    ("RENEW",    "再生可能エネルギー", "Renewable Energy",      "ENERGY",  2, "太陽光・風力・地熱・電力小売",                  31),
    ("BATTERY",  "電池・蓄電",         "Battery & Storage",     "ENERGY",  2, "EV電池・定置用蓄電・材料",                      32),
    ("HYDROGEN", "水素・アンモニア",   "Hydrogen",              "ENERGY",  2, "水素製造・輸送・活用・アンモニア",               33),

    # ── 小分類: CONSUMER ────────────────────
    ("INBOUND",  "インバウンド消費",   "Inbound Tourism",       "CONSUMER",2, "百貨店・免税・観光・エンタメ",                  41),
    ("HEALTH_DX","医療DX",             "Healthcare DX",         "CONSUMER",2, "電子カルテ・医療プラットフォーム",              42),

    # ── 小分類: MOBILITY ────────────────────
    ("EV",       "EV・次世代車",       "Electric Vehicles",     "MOBILITY",2, "EV・HEV・部材・充電インフラ",                   51),
]

# ─────────────────────────────────────────
# 2. 銘柄×テーマ マッピング
#    (code, theme_code, relevance, note)
#
# ★変更履歴（メンテ時に追記）:
#   2026-06-27: SPACE/DRONEを分離。PHYS_AI新設。6723ルネサスをSEMI追加。
#               9348/9412/402A/4241を宇宙テーマに追加。290A/464AをSEMIコアへ昇格。
# ─────────────────────────────────────────
MAPPINGS = [

    # ══ 生成AI・LLM ══════════════════════════
    ("3778", "AI_GEN",   3, "GPUクラウド「高火力」提供。生成AI向けインフラが主力"),
    ("9984", "AI_GEN",   3, "ARM・AI関連企業に大規模投資。Stargate参画"),
    ("2158", "AI_GEN",   3, "NLP AI「KIBIT」が主力。法務・コンプライアンスAI"),
    ("3905", "AI_GEN",   3, "コンピュータビジョンAIが主力事業"),
    ("3636", "AI_GEN",   2, "AI戦略コンサルティング・研究開発"),
    ("3626", "AI_GEN",   2, "AI開発サービス・SIer"),
    ("6701", "AI_GEN",   2, "生成AIプラットフォーム「cotomi」提供"),
    ("6702", "AI_GEN",   2, "AI「Fujitsu Kozuchi」。企業向けAI DX"),
    ("4307", "AI_GEN",   2, "AI活用DXコンサルティング"),
    ("3853", "AI_GEN",   2, "AIデータ連携ミドルウェア「ASTERIA」"),
    ("3915", "AI_GEN",   2, "Salesforce基盤のAI・クラウド開発"),
    ("2130", "AI_GEN",   2, "AIマーケティング支援"),
    ("3565", "AI_GEN",   2, "AI・DX導入支援"),
    ("3300", "AI_GEN",   2, "DX・AI事業"),
    ("2321", "AI_GEN",   1, "音声AI技術を保有"),
    ("3132", "AI_GEN",   1, "AIチップ・GPU販売"),

    # ══ AIインフラ・DC ════════════════════════
    ("3778", "AI_INFRA", 3, "AI向けGPUデータセンターを国内展開"),
    ("3774", "AI_INFRA", 3, "国内大手ISP。独自DCを運営"),
    ("1951", "AI_INFRA", 2, "DC向け通信工事・電気工事大手"),
    ("1944", "AI_INFRA", 2, "DC向け電気工事（きんでん）"),
    ("1969", "AI_INFRA", 2, "DC冷却システム・空調設備（高砂熱）"),
    ("1952", "AI_INFRA", 2, "DC空調設備（新日本空調）"),
    ("1961", "AI_INFRA", 2, "DC設備工事（三機工業）"),
    ("1982", "AI_INFRA", 2, "DC設備工事（日比谷総合設備）"),
    ("1417", "AI_INFRA", 2, "DC向け通信インフラ工事（ミライトワン）"),
    ("1721", "AI_INFRA", 2, "DC向け通信インフラ工事（コムシスHD）"),
    ("6367", "AI_INFRA", 2, "DC大型冷却システムが急拡大（ダイキン）"),
    ("1925", "AI_INFRA", 2, "DC建設・運営事業（大和ハウス）"),
    ("2327", "AI_INFRA", 2, "クラウドデータ管理・DCサービス（NSSOL）"),
    # 9613 NTTデータG → DBに未登録のため除外（社名変更で管理コード要確認）
    # 1832 北海電工 → DBに未登録のため除外
    ("1942", "AI_INFRA", 1, "DC向け電気工事（関電工）"),
    ("1980", "AI_INFRA", 1, "DC設備工事（ダイダン）"),

    # ══ 半導体 ════════════════════════════════
    ("8035", "SEMI",     3, "製造装置国内最大手（東京エレクトロン）"),
    ("6857", "SEMI",     3, "半導体テスター世界首位（アドバンテスト）"),
    ("6146", "SEMI",     3, "ダイシング装置世界首位（ディスコ）"),
    ("7735", "SEMI",     3, "洗浄装置・塗布現像装置大手（SCREEN HD）"),
    ("4063", "SEMI",     3, "シリコンウェーハ・フォトレジスト（信越化学）"),
    ("3436", "SEMI",     3, "シリコンウェーハ世界2位（SUMCO）"),
    ("285A", "SEMI",     3, "NAND型フラッシュメモリ大手（キオクシア）"),
    ("4186", "SEMI",     3, "半導体用フォトレジスト（東応化工業）"),
    ("4369", "SEMI",     3, "プロセスガス・材料（トリケミカル）"),
    ("6723", "SEMI",     3, "マイコン・SoC。車載・産業向け世界大手（ルネサス）"),  # ★追加
    ("4980", "SEMI",     2, "半導体製造向け材料（デクセリアルズ）"),
    ("6235", "SEMI",     2, "成膜装置（オプトランHD）"),
    ("5016", "SEMI",     2, "電解銅箔・半導体向け金属（JX金属）"),
    ("6125", "SEMI",     2, "シリコンウェーハ研削装置（岡本工作機械）"),
    ("4062", "SEMI",     2, "半導体パッケージ基板（イビデン）"),
    ("5201", "SEMI",     2, "半導体用ガラス基板・材料（AGC）"),
    ("3132", "SEMI",     2, "半導体商社・セキュリティ（マクニカHD）"),
    ("3321", "SEMI",     2, "半導体・電子部品商社（ミタチ産業）"),
    ("3445", "SEMI",     2, "シリコンウェーハリサイクル（RSテクノ）"),
    ("4187", "SEMI",     2, "半導体プロセス材料（大有機化学）"),
    ("3652", "SEMI",     1, "半導体設計EDA（DMP）"),

    # ══ サイバーセキュリティ ════════════════
    ("3692", "CYBER",    3, "エンドポイントセキュリティ（FFRI）"),
    ("3040", "CYBER",    3, "ネットワーク・ゼロトラストセキュリティ（ソリトン）"),
    ("3042", "CYBER",    3, "MDR・SOCサービス（セキュアヴェイル）"),
    ("2326", "CYBER",    3, "Webフィルタリング・情報漏洩対策（デジタルアーツ）"),
    ("3682", "CYBER",    3, "特権ID管理・内部不正対策（エンカレッジ）"),
    ("153A", "CYBER",    3, "AI不正アクセス検知（カウリス）"),
    ("338A", "CYBER",    2, "量子暗号・セキュリティ（ゼンムテック）"),
    ("3562", "CYBER",    2, "ランサムウェア対策・バックアップ（No.1）"),
    ("3132", "CYBER",    2, "セキュリティ製品販売（マクニカHD）"),
    ("2327", "CYBER",    2, "セキュリティサービス（NSSOL）"),
    ("3697", "CYBER",    2, "ソフトウェアテスト・セキュリティ（SHIFT）"),
    ("3676", "CYBER",    2, "セキュリティ事業（デジハHD）"),
    # 2467 VLCセキュリティ → DBに未登録のため除外
    ("173A", "CYBER",    2, "エンドポイントセキュリティ（ハンモック）"),
    ("155A", "CYBER",    2, "セキュリティIT人材（情報戦略テクノロジー）"),

    # ══ ロボット・FA ════════════════════════
    # ★ 6232 ACSL・218A Liberawere はドローンテーマへ移動
    ("6954", "ROBOT",    3, "産業ロボット・CNC装置世界首位（ファナック）"),
    ("6506", "ROBOT",    3, "産業ロボット大手（安川電機）"),
    ("6268", "ROBOT",    3, "精密減速機世界首位（ナブテスコ）"),
    ("3443", "ROBOT",    2, "建設ロボット・橋梁（川田テクノロジーズ）"),
    ("3741", "ROBOT",    2, "ロボット制御ソフトウェア（セック）"),
    ("3652", "ROBOT",    2, "ロボット組み込みOS（DMP）"),
    ("6481", "ROBOT",    2, "リニアガイド・ロボット部品（THK）"),
    ("6464", "ROBOT",    2, "ボールスクリュー（ツバキナカシマ）"),
    ("6471", "ROBOT",    2, "精密ベアリング（日本精工）"),
    ("6302", "ROBOT",    1, "産業機械・ロボット周辺（住友重機械）"),

    # ══ クラウド・SaaS ═══════════════════════
    ("3778", "CLOUD",    2, "クラウドサービス（さくらインターネット）"),
    ("3774", "CLOUD",    3, "クラウド・ネットワーク（IIJ）"),
    ("3915", "CLOUD",    3, "Salesforceクラウド受託開発（テラスカイ）"),
    ("3626", "CLOUD",    2, "クラウドSaaS開発・運用（TIS）"),
    ("3853", "CLOUD",    2, "データ連携SaaS「ASTERIA」（アステリア）"),
    ("3900", "CLOUD",    2, "クラウドワーキング（クラウドワークス）"),
    # 9613 NTTデータG → DBに未登録のため除外（コード変更で要確認）
    ("4307", "CLOUD",    2, "クラウドDXコンサル（野村総研）"),
    ("3625", "CLOUD",    2, "クラウドSaaS（テックファーム）"),

    # ══ フィジカルAI ═════════════════════════
    # AI×物理世界: 産業ロボット・工場自動化・組み込みAI・ARM生態系
    ("9984", "PHYS_AI",  3, "ARM Holdings筆頭株主。物理AI向けチップの設計基盤（ソフトバンクG）"),
    ("6506", "PHYS_AI",  3, "AI制御産業ロボット「MOTOMAN」世界大手（安川電機）"),
    ("6954", "PHYS_AI",  3, "AI搭載CNC・産業ロボット世界首位（ファナック）"),
    ("6501", "PHYS_AI",  3, "産業AI基盤「Lumada」・スマートファクトリー（日立製作所）"),
    ("6503", "PHYS_AI",  2, "FAシステム・PLCで国内首位クラス。産業用AIエッジ（三菱電機）"),
    ("6268", "PHYS_AI",  2, "ロボット関節向け精密減速機世界首位（ナブテスコ）"),
    ("6302", "PHYS_AI",  2, "射出成型機・産業機械のAI制御化（住友重機械工業）"),
    ("5406", "PHYS_AI",  2, "鉄鋼・アルミ材料でロボット・EV向け素材供給（神戸製鋼所）"),
    ("6723", "PHYS_AI",  2, "車載・産業向けマイコンSoCでロボットAI制御を支える（ルネサス）"),
    ("4664", "PHYS_AI",  1, "製造業向けエンジニアリングサービス・FA周辺（アールエスシー）"),

    # ══ 防衛装備 ══════════════════════════════
    ("7011", "DEF_EQ",   3, "防衛省最大サプライヤー。艦艇・ミサイル・戦闘機"),
    ("7012", "DEF_EQ",   3, "潜水艦・護衛艦・哨戒機・ヘリ（川崎重工）"),
    ("7013", "DEF_EQ",   3, "航空エンジン・ロケット・艦艇機器（IHI）"),
    ("6203", "DEF_EQ",   3, "89式小銃・20mm機関砲製造（豊和工業）"),
    ("7721", "DEF_EQ",   3, "防衛電子機器・ジャイロ（東京計器）"),
    ("7224", "DEF_EQ",   3, "US-2飛行艇・特装車（新明和工業）"),
    ("4274", "DEF_EQ",   3, "照明弾・曳光弾・火工品（細谷火工）"),
    ("5631", "DEF_EQ",   2, "火砲・装甲鋼板（日本製鋼所）"),
    ("4275", "DEF_EQ",   2, "火工品・産業用爆薬（カーリット）"),
    ("6268", "DEF_EQ",   2, "航空機用アクチュエータ（ナブテスコ）"),
    ("6503", "DEF_EQ",   2, "防衛電子・誘導兵器（三菱電機）"),
    ("6701", "DEF_EQ",   2, "防衛通信・レーダーシステム（NEC）"),
    ("7409", "DEF_EQ",   2, "航空エンジン部品（エアロエッジ）"),
    ("7014", "DEF_EQ",   2, "艦艇建造（名村造船所）"),
    ("6946", "DEF_EQ",   2, "赤外線センサー・防衛電子（日本アビオニクス）"),
    ("6208", "DEF_EQ",   2, "機雷・水中防衛装備（石川製作所）"),
    ("6838", "DEF_EQ",   2, "防衛通信部品（多摩川HD）"),
    # 6111 旭精機工業 → DBに未登録のため除外
    ("3692", "DEF_EQ",   1, "サイバー防衛・防衛省向けセキュリティ（FFRI）"),

    # ══ 宇宙 ═════════════════════════════════
    # ★ ドローン銘柄（278A/6232）は DRONE テーマへ移動
    ("186A", "SPACE",    3, "軌道上サービス専業。デブリ除去が主力（アストロスケールHD）"),
    ("9348", "SPACE",    3, "月面探査・月面物流。HAKUTO-R（ispace）"),           # ★追加
    ("9412", "SPACE",    3, "衛星通信国内最大手。スカパー!放送・宇宙通信（スカパーJSAT）"),  # ★追加
    ("402A", "SPACE",    3, "超小型衛星群による地球観測データビジネス（アクセルスペースHD）"),  # ★追加
    ("290A", "SPACE",    3, "SAR衛星による地球観測・防衛情報（Synspective）"),   # 関連→コアへ昇格
    ("464A", "SPACE",    3, "小型SAR衛星コンステレーション（QPS研究所）"),        # 関連→コアへ昇格
    ("4241", "SPACE",    2, "宇宙機器・半導体向け特殊フィルム材料（アテクト）"), # ★追加
    ("7011", "SPACE",    2, "H3ロケット主契約社（三菱重工）"),
    ("7012", "SPACE",    2, "人工衛星・宇宙機器製造（川崎重工）"),
    ("7013", "SPACE",    2, "H3ロケットエンジン「LE-9」製造（IHI）"),
    ("7701", "SPACE",    2, "宇宙・航空計測機器（島津製作所）"),

    # ══ ドローン ══════════════════════════════
    # ★ 宇宙テーマから独立。産業・防衛ドローンに特化
    ("278A", "DRONE",    3, "ドローン点検・測量・物流サービス国内大手（テラドローン）"),
    ("6232", "DRONE",    3, "防衛・産業用自律飛行ロボット専業（ACSL）"),
    ("218A", "DRONE",    3, "狭小空間点検用小型ドローン専業（リベラウェア）"),
    ("3741", "DRONE",    2, "ドローン・ロボット制御ソフトウェア（セック）"),
    ("7011", "DRONE",    2, "防衛省向けドローン・UAS開発（三菱重工）"),
    ("7013", "DRONE",    2, "小型ターボジェットドローン推進系開発（IHI）"),
    ("6268", "DRONE",    1, "ドローン用小型精密アクチュエータ（ナブテスコ）"),

    # ══ 再生可能エネルギー ════════════════════
    ("1407", "RENEW",    3, "太陽光発電所開発・運営（ウエストHD）"),
    ("1436", "RENEW",    3, "再エネ電力開発（グリーンエナジー）"),
    ("3150", "RENEW",    3, "再エネ電力小売・省エネ（グリムス）"),
    ("9517", "RENEW",    3, "再エネ電力発電・小売（イーレックス）"),
    ("5074", "RENEW",    3, "再エネ発電・蓄電事業（テスHD）"),
    ("2311", "RENEW",    2, "太陽光発電保守・オール電化（エプコ）"),
    ("1963", "RENEW",    2, "再エネプラント設計・建設（日揮HD）"),
    ("1925", "RENEW",    2, "太陽光発電・再エネ事業（大和ハウス）"),
    ("3113", "RENEW",    2, "再エネ関連（UNIVA）"),
    ("1711", "RENEW",    1, "太陽光・自然エネ関連（SDSHD）"),

    # ══ 電池・蓄電 ════════════════════════════
    ("6752", "BATTERY",  3, "EV用円筒形電池・住宅用蓄電システム（パナソニックHD）"),
    ("485A", "BATTERY",  3, "大型蓄電システム専業（PowerX）"),
    ("5802", "BATTERY",  2, "レドックスフロー電池・ワイヤーハーネス（住友電気工業）"),
    ("1436", "BATTERY",  2, "蓄電池＋再エネセット販売（グリーンエナジー）"),
    ("3150", "BATTERY",  2, "蓄電池販売・電力サービス（グリムス）"),
    ("3825", "BATTERY",  2, "蓄電池販売（リミックスポイント）"),
    ("5074", "BATTERY",  2, "蓄電・再エネ設備（テスHD）"),
    ("3913", "BATTERY",  2, "蓄電池販売（グリーンビーン）"),
    ("3863", "BATTERY",  1, "電池用セパレーター材料（日本製紙）"),

    # ══ 水素・アンモニア ═══════════════════════
    ("4088", "HYDROGEN", 3, "水素ステーション・液化水素供給（エア・ウォーター）"),
    ("4091", "HYDROGEN", 3, "水素・産業ガス（日本酸素HD）"),
    ("4097", "HYDROGEN", 3, "水素ガス供給（高圧ガス工業）"),
    ("4093", "HYDROGEN", 3, "水素・アセチレン供給（アセチレン）"),
    ("3407", "HYDROGEN", 3, "アルカリ水電解装置（グリーン水素）（旭化成）"),
    ("4043", "HYDROGEN", 2, "グリーン水素製造（トクヤマ）"),
    ("4047", "HYDROGEN", 2, "水電解・水素製造（関東電化工業）"),
    ("1963", "HYDROGEN", 2, "水素・アンモニアプラント設計（日揮HD）"),
    ("3402", "HYDROGEN", 2, "水素分離膜・炭素繊維タンク（東レ）"),
    ("4114", "HYDROGEN", 2, "水素化学品・触媒（日本触媒）"),
    ("4403", "HYDROGEN", 2, "アンモニア誘導体・含エネルギー材料（日油）"),
    ("1802", "HYDROGEN", 1, "水素インフラ建設工事（大林組）"),
    ("1803", "HYDROGEN", 1, "水素設備建設工事（清水建設）"),

    # ══ インバウンド消費 ═══════════════════════
    ("3099", "INBOUND",  3, "三越・伊勢丹。免税売上が収益の大きな柱"),
    ("8233", "INBOUND",  3, "高島屋。百貨店免税・海外出店"),
    ("3086", "INBOUND",  3, "大丸・松坂屋。免税売上比率が高い（Jフロント）"),
    ("8136", "INBOUND",  2, "サンリオキャラクター。外国人ファン需要旺盛"),
    ("2780", "INBOUND",  2, "中古ブランド品の訪日客購入（コメ兵HD）"),
    ("4680", "INBOUND",  2, "ボーリング・アミューズメント。外国人客増（ラウンドワン）"),
    ("3048", "INBOUND",  2, "家電量販店・免税販売（ビックカメラ）"),
    ("2670", "INBOUND",  2, "靴チェーン・免税販売（ABCマート）"),
    ("9602", "INBOUND",  2, "映画・演劇。訪日客のエンタメ消費（東宝）"),
    ("2222", "INBOUND",  2, "土産菓子「鳥取・山陰」訪日客土産（寿スピリッツ）"),
    ("3132", "INBOUND",  1, "電子機器販売・外国人需要（マクニカHD）"),

    # ══ 医療DX ═══════════════════════════════
    ("2413", "HEALTH_DX",3, "医師向けプラットフォームm3.com。国内医師の91%が登録（エムスリー）"),
    ("4480", "HEALTH_DX",3, "クラウド電子カルテCLINICS・医療人材（メドレー）"),
    ("9341", "HEALTH_DX",3, "医療機関DX・患者サービス（GENOVA）"),
    ("3628", "HEALTH_DX",3, "医療レセプトビッグデータ分析（データホライゾン）"),
    ("6701", "HEALTH_DX",2, "医療情報システム・電子カルテ（NEC）"),
    ("6702", "HEALTH_DX",2, "病院情報システム（富士通）"),
    ("4307", "HEALTH_DX",2, "医療DXコンサル・システム（野村総研）"),

    # ══ EV・次世代車 ══════════════════════════
    ("6752", "EV",       3, "パナソニックエナジーがEV電池（テスラ向け）を主力供給"),
    ("7203", "EV",       2, "HEV・PHEV・BEVの全方位電動化戦略（トヨタ）"),
    ("7267", "EV",       2, "Honda 0シリーズ。EV本格展開（ホンダ）"),
    ("6902", "EV",       2, "車載電装・電動化部品大手（デンソー）"),
    ("5802", "EV",       2, "EV用ワイヤーハーネス・バッテリーシステム（住友電気工業）"),
    ("6471", "EV",       2, "EV駆動モータ向けベアリング（日本精工）"),
    ("6481", "EV",       2, "EV製造ラインLM・リニアアクチュエータ（THK）"),
    ("6472", "EV",       2, "EV向けハブベアリング（NTN）"),
    ("5019", "EV",       2, "全固体電池材料（トヨタと協業）（出光興産）"),
    ("4062", "EV",       2, "EV向けパッケージ基板・セラミックス（イビデン）"),
    ("4169", "EV",       2, "EV充電インフラ（エネチェンジ）"),
    ("6506", "EV",       2, "EV製造向け産業ロボット（安川電機）"),
    ("5985", "EV",       2, "EV向けばね部品（サンコール）"),
    ("6504", "EV",       2, "EV向けパワー半導体・インバータ（富士電機）"),
    ("4063", "EV",       1, "EV向け機能性材料・シリコーン（信越化学）"),
    ("3407", "EV",       1, "EV向けエンジニアリングプラスチック（旭化成）"),
]

# ─────────────────────────────────────────
# 3. DB投入
# ─────────────────────────────────────────

def upsert_categories(cur) -> dict[str, int]:
    """テーマカテゴリを投入し、code→id マップを返す。"""
    large = [(r[0], r[1], r[2], None, r[4], r[5], r[6], True)
             for r in CATEGORIES if r[3] is None]
    bulk_upsert(cur,
        "theme_categories",
        ["code","name","name_en","parent_id","level","description","sort_order","is_active"],
        large,
        update_cols=["name","name_en","level","description","sort_order","is_active"])

    cur.execute("SELECT code, id FROM theme_categories")
    code2id = {r[0]: r[1] for r in cur.fetchall()}

    small = [(r[0], r[1], r[2], code2id[r[3]], r[4], r[5], r[6], True)
             for r in CATEGORIES if r[3] is not None]
    bulk_upsert(cur,
        "theme_categories",
        ["code","name","name_en","parent_id","level","description","sort_order","is_active"],
        small,
        update_cols=["name","name_en","parent_id","level","description","sort_order","is_active"])

    cur.execute("SELECT code, id FROM theme_categories")
    return {r[0]: r[1] for r in cur.fetchall()}


def upsert_mappings(cur, code2id: dict[str, int]):
    """stock_themes を投入（新規・変更のみ。削除は --sync で実行）。"""
    rows = [(m[0], code2id[m[1]], m[2], m[3]) for m in MAPPINGS if m[1] in code2id]
    bulk_upsert(cur,
        "stock_themes",
        ["code","theme_id","relevance","note"],
        rows,
        update_cols=["relevance","note"])
    return len(rows)


def sync_mappings(cur, code2id: dict[str, int]) -> int:
    """
    MAPPINGSに存在しないstock_themes行を削除する（完全同期）。
    テーマ移動・銘柄削除を確実に反映したい場合に使用。
    """
    # 現在の正規マッピングセット
    valid = {(m[0], code2id[m[1]]) for m in MAPPINGS if m[1] in code2id}

    cur.execute("SELECT id, code, theme_id FROM stock_themes")
    to_delete = [r[0] for r in cur.fetchall() if (r[1], r[2]) not in valid]

    if to_delete:
        placeholders = ",".join(["%s"] * len(to_delete))
        cur.execute(f"DELETE FROM stock_themes WHERE id IN ({placeholders})", to_delete)
        print(f"  削除: {len(to_delete)} 件（MAPPINGSに存在しない行）")
    else:
        print("  削除対象なし")

    return len(to_delete)


def check_unknown_codes(cur):
    """DBの stocks テーブルに存在しないコードを報告。"""
    mapped_codes = list({m[0] for m in MAPPINGS})
    placeholders = ",".join(["%s"] * len(mapped_codes))
    cur.execute(f"SELECT code FROM stocks WHERE code IN ({placeholders})", mapped_codes)
    found = {r[0] for r in cur.fetchall()}
    missing = sorted(set(mapped_codes) - found)
    if missing:
        print(f"\n未登録コード（stocks テーブルに存在しない）: {len(missing)} 件")
        for c in missing:
            labels = [m[1] for m in MAPPINGS if m[0] == c]
            print(f"  {c}  → テーマ: {labels}")
    else:
        print("全コードが stocks テーブルに存在します。")


def print_summary(cur):
    """テーマ別銘柄数サマリーを表示。"""
    cur.execute("""
        SELECT tc.name, COUNT(st.id) AS cnt,
               SUM(st.relevance=3) AS core,
               SUM(st.relevance=2) AS rel,
               SUM(st.relevance=1) AS peri
        FROM theme_categories tc
        LEFT JOIN stock_themes st ON tc.id = st.theme_id
        WHERE tc.level = 2
        GROUP BY tc.id, tc.name
        ORDER BY tc.sort_order
    """)
    print(f"\n{'テーマ':<20}  {'銘柄数':>5}  {'コア':>4}  {'関連':>4}  {'周辺':>4}")
    print("-" * 47)
    for name, cnt, core, rel, peri in cur.fetchall():
        print(f"{name:<20}  {cnt or 0:>5}  {int(core or 0):>4}  {int(rel or 0):>4}  {int(peri or 0):>4}")


def main():
    check_only = "--check" in sys.argv
    do_sync    = "--sync"  in sys.argv

    conn = get_conn()
    cur  = conn.cursor()

    if check_only:
        print("=== 未登録コードチェック ===")
        check_unknown_codes(cur)
        cur.close()
        conn.close()
        return

    print("=== テーマカテゴリ投入 ===")
    code2id = upsert_categories(cur)
    conn.commit()
    print(f"  {len(code2id)} カテゴリ（大分類+小分類）を登録")

    print("\n=== 銘柄×テーマ マッピング投入 ===")
    n = upsert_mappings(cur, code2id)
    conn.commit()
    print(f"  {n} 件を登録/更新")

    if do_sync:
        print("\n=== 不要マッピング削除（--sync）===")
        sync_mappings(cur, code2id)
        conn.commit()

    print("\n=== コード照合 ===")
    check_unknown_codes(cur)

    print_summary(cur)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
