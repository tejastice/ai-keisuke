import discord
from discord.ext import commands
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
import urllib.parse
import requests
from datetime import datetime, timezone, timedelta
import logging
import asyncio
import tempfile
from pydub import AudioSegment
from PIL import Image, ImageDraw, ImageFont
import random
import re
import io
import aiohttp

# スクリプトのディレクトリを基準に.envファイルを読み込む
script_dir = Path(__file__).parent
env_path = script_dir / '.env'

# 必要なディレクトリを自動作成
def create_required_directories():
    """起動時に必要なディレクトリを自動作成"""
    required_dirs = [
        script_dir / "data" / "server_data",
        script_dir / "data" / "user_data", 
        script_dir / "attachments"
    ]
    
    for dir_path in required_dirs:
        dir_path.mkdir(parents=True, exist_ok=True)
        print(f"📁 ディレクトリ確認: {dir_path}")

# 必要なディレクトリを作成
create_required_directories()

# 既存の環境変数をクリアしてから.envファイルを読み込む
if 'OPENAI_API_KEY' in os.environ:
    del os.environ['OPENAI_API_KEY']
if 'DISCORD_BOT_TOKEN' in os.environ:
    del os.environ['DISCORD_BOT_TOKEN']

load_dotenv(env_path, override=True)

# 環境変数からトークンを取得
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# OpenAIモデル設定
FREE_USER_MODEL = "gpt-4.1-mini"
PREMIUM_USER_MODEL = "gpt-4.1"

# テストサーバーID（スラッシュコマンドの即座反映用）
# Botが参加しているサーバーのIDに変更してください
TEST_GUILD_ID = 1383696841450721442  # Botがこのサーバーに招待されている必要があります

# settings.jsonから設定を読み込む
settings_path = script_dir / "settings.json"
if settings_path.exists():
    with open(settings_path, 'r', encoding='utf-8') as f:
        settings = json.load(f)
        FREE_USER_DAILY_LIMIT = settings.get("free_user_daily_limit", 5)
else:
    FREE_USER_DAILY_LIMIT = 5  # デフォルト値

# カスタムログハンドラー（書き込み時のみファイルを開く）
class SyncFriendlyFileHandler(logging.Handler):
    def __init__(self, filename, encoding='utf-8', max_bytes=10*1024*1024):
        super().__init__()
        self.filename = filename
        self.encoding = encoding
        self.max_bytes = max_bytes
        
    def emit(self, record):
        try:
            # ファイルサイズチェック（ローテーション）
            if Path(self.filename).exists() and Path(self.filename).stat().st_size > self.max_bytes:
                self._rotate_logs()
            
            # 書き込み時のみファイルを開く
            with open(self.filename, 'a', encoding=self.encoding) as f:
                f.write(self.format(record) + '\n')
                f.flush()  # 即座に書き込み
        except Exception:
            self.handleError(record)
    
    def _rotate_logs(self):
        """ログファイルをローテーション"""
        try:
            base_path = Path(self.filename)
            # 古いバックアップを削除・移動
            for i in range(4, 0, -1):  # log.txt.4 → log.txt.5
                old_file = base_path.with_suffix(f'.txt.{i}')
                new_file = base_path.with_suffix(f'.txt.{i+1}')
                if old_file.exists():
                    if new_file.exists():
                        new_file.unlink()
                    old_file.rename(new_file)
            
            # 現在のログファイルをlog.txt.1に移動
            if base_path.exists():
                backup_file = base_path.with_suffix('.txt.1')
                if backup_file.exists():
                    backup_file.unlink()
                base_path.rename(backup_file)
        except Exception as e:
            print(f"ログローテーションエラー: {e}")

# ログ設定（同期フレンドリー）
log_file = script_dir / "log.txt"
sync_handler = SyncFriendlyFileHandler(log_file)
sync_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    level=logging.INFO,
    handlers=[
        sync_handler,
        logging.StreamHandler()  # コンソールにも出力
    ]
)
logger = logging.getLogger(__name__)

# 統計管理クラス
class StatsManager:
    def __init__(self):
        self.stats_dir = script_dir / "data" / "activity_logs"
        self.stats_dir.mkdir(exist_ok=True)
        logger.info("統計管理システムを初期化しました")
    
    async def record_user_activity(self, user_id, bot_instance=None):
        """ユーザーアクティビティをリアルタイム記録"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            log_file = self.stats_dir / f"{today}.json"
            
            # 今日のログを読み込み
            if log_file.exists():
                with open(log_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                # 新しい日の最初の記録時にサーバー数を記録
                server_count = len(bot_instance.guilds) if bot_instance else 0
                data = {
                    "date": today,
                    "active_users": [],
                    "total_actions": 0,
                    "server_count": server_count
                }
                logger.info(f"新しい日の統計開始: サーバー数 {server_count}")
            
            # ユーザーを追加（重複なし）
            if user_id not in data["active_users"]:
                data["active_users"].append(user_id)
                logger.debug(f"新規アクティブユーザー記録: {user_id}")
            
            data["total_actions"] += 1
            
            # ログファイルに保存
            with open(log_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            logger.error(f"アクティビティ記録エラー: {e}")
    
    def calculate_dau(self, target_date=None):
        """指定日のDAU計算（デフォルトは今日）"""
        try:
            if target_date is None:
                target_date = datetime.now().strftime("%Y-%m-%d")
            
            log_file = self.stats_dir / f"{target_date}.json"
            
            if log_file.exists():
                with open(log_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return len(data.get("active_users", []))
            return 0
            
        except Exception as e:
            logger.error(f"DAU計算エラー: {e}")
            return 0
    
    def calculate_mau(self, target_date=None):
        """指定日から過去30日間のMAU計算"""
        try:
            if target_date is None:
                base_date = datetime.now()
            else:
                base_date = datetime.strptime(target_date, "%Y-%m-%d")
            
            mau_users = set()
            
            for i in range(30):
                date = (base_date - timedelta(days=i)).strftime("%Y-%m-%d")
                log_file = self.stats_dir / f"{date}.json"
                
                if log_file.exists():
                    with open(log_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        mau_users.update(data.get("active_users", []))
            
            return len(mau_users)
            
        except Exception as e:
            logger.error(f"MAU計算エラー: {e}")
            return 0
    
    def get_stats_summary(self):
        """統計サマリーを取得"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            dau = self.calculate_dau()
            mau = self.calculate_mau()
            
            # 総アクション数・サーバー数（今日）
            today_log = self.stats_dir / f"{today}.json"
            total_actions_today = 0
            server_count_today = 0
            if today_log.exists():
                with open(today_log, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    total_actions_today = data.get("total_actions", 0)
                    server_count_today = data.get("server_count", 0)
            
            return {
                "date": today,
                "dau": dau,
                "mau": mau,
                "total_actions_today": total_actions_today,
                "server_count": server_count_today
            }
            
        except Exception as e:
            logger.error(f"統計サマリー取得エラー: {e}")
            return {"date": "", "dau": 0, "mau": 0, "total_actions_today": 0, "server_count": 0}

# OpenAIクライアントの初期化（60秒タイムアウト設定）
client_openai = None
if OPENAI_API_KEY:
    client_openai = OpenAI(
        api_key=OPENAI_API_KEY,
        timeout=180.0  # 180秒タイムアウト（長い音声ファイル対応）
    )


# Intentsの設定
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

# Botの初期化
bot = commands.Bot(command_prefix='!', intents=intents)

# 統計管理インスタンスを作成
stats_manager = StatsManager()


def load_server_data(server_id):
    """サーバーデータを読み込む"""
    file_path = script_dir / "data" / "server_data" / f"{server_id}.json"
    if file_path.exists():
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def save_server_data(server_id, data):
    """サーバーデータを保存する"""
    data_dir = script_dir / "data" / "server_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    file_path = data_dir / f"{server_id}.json"
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_channel_active(server_id, channel_id):
    """チャンネルが有効かどうかをチェック"""
    server_data = load_server_data(server_id)
    if server_data and 'active_channel_ids' in server_data:
        return str(channel_id) in server_data['active_channel_ids']
    return False

def migrate_user_data(user_data, user_id, username):
    """古いユーザーデータを新しいフォーマットにマイグレーション"""
    # 必要なフィールドのデフォルト値
    default_fields = {
        "user_id": str(user_id),
        "username": username,
        "custom_prompt_x_post": "",
        "custom_prompt_article": "",
        "custom_prompt_memo": "",
        "status": "free",
        "last_used_date": "",
        "daily_usage_count": 0
    }
    
    # 不足しているフィールドを追加
    updated = False
    for field, default_value in default_fields.items():
        if field not in user_data:
            user_data[field] = default_value
            updated = True
            logger.info(f"マイグレーション: ユーザー {user_id} に {field} フィールドを追加")
    
    # 古いフィールド名の変換
    if "custom_x_post_prompt" in user_data:
        user_data["custom_prompt_x_post"] = user_data.pop("custom_x_post_prompt")
        updated = True
        logger.info(f"マイグレーション: ユーザー {user_id} の custom_x_post_prompt を custom_prompt_x_post に変換")
    
    return user_data, updated

async def check_content_for_urls(content_text, user, channel):
    """コンテンツ内のURLを検出し、必要に応じて警告を表示"""
    import re
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, content_text) if content_text else []
    
    if urls:
        warning_msg = (
            f"{user.mention} ⚠️ URLが含まれたコンテンツを検出しました\n"
            f"📝 URLの中身は読み取ることができませんが、このまま処理を続行します\n"
            f"🔗 検出されたURL: {len(urls)}個"
        )
        await channel.send(warning_msg)
    
    return content_text

def load_user_data(user_id):
    """ユーザーデータを読み込む"""
    file_path = script_dir / "data" / "user_data" / f"{user_id}.json"
    if file_path.exists():
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"ユーザーデータ読み込みエラー {user_id}: {e}")
            return None
    return None

def save_user_data(user_id, data):
    """ユーザーデータを保存する"""
    data_dir = script_dir / "data" / "user_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    file_path = data_dir / f"{user_id}.json"
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

async def is_premium_user(user_id):
    """ユーザーがプレミアムかどうかを判定"""
    try:
        # サーバーオーナーの特別判定
        community_guild = bot.get_guild(int(settings.get("community_server_id")))
        if not community_guild:
            logger.warning(f"Community server not found: {settings.get('community_server_id')}")
            return False
        
        # オーナーチェック（Discord APIベース）
        if int(user_id) == community_guild.owner_id:
            logger.info(f"User {user_id} is server owner - granting premium access")
            return True
        
        # オーナーチェック（設定ファイルベース）
        owner_user_id = settings.get("owner_user_id")
        if owner_user_id and str(user_id) == str(owner_user_id):
            logger.info(f"User {user_id} is configured owner - granting premium access")
            return True
        
        logger.info(f"Debug: Checking user {user_id} in guild {community_guild.name}")
        
        member = community_guild.get_member(int(user_id))
        if not member:
            logger.info(f"User {user_id} not found in cache, trying to fetch from API...")
            try:
                # キャッシュにない場合はAPIから直接取得を試す
                member = await community_guild.fetch_member(int(user_id))
                logger.info(f"Successfully fetched user {user_id} from API")
            except discord.NotFound:
                logger.warning(f"User {user_id} not found in community server {community_guild.name}")
                logger.info(f"Debug: Guild has {community_guild.member_count} members")
                return False
            except discord.Forbidden:
                logger.warning(f"Permission denied when fetching user {user_id} from community server")
                logger.info(f"Debug: This may be due to insufficient bot permissions")
                return False
            except Exception as e:
                logger.error(f"Error fetching user {user_id}: {e}")
                return False
        
        logger.info(f"Debug: Found member {member.name}#{member.discriminator}")
        logger.info(f"Debug: Member roles: {[f'{role.name}({role.id})' for role in member.roles]}")
        
        # プレミアムロールの確認
        premium_role_id = int(settings.get("premium_role_id"))
        logger.info(f"Debug: Looking for premium role ID: {premium_role_id}")
        
        has_premium_role = any(role.id == premium_role_id for role in member.roles)
        
        logger.info(f"Premium check for user {user_id} ({member.name}): {has_premium_role}")
        return has_premium_role
        
    except Exception as e:
        logger.error(f"Error checking premium status for user {user_id}: {e}")
        return False

def can_use_feature(user_data, is_premium):
    """機能使用可能かチェックし、使用回数を更新"""
    # 日本時間（JST）で現在の日付を取得
    jst = timezone(timedelta(hours=9))
    today = datetime.now(jst).strftime("%Y-%m-%d")
    
    # プレミアムユーザーは無制限（ただし使用回数はカウント）
    if is_premium:
        last_used_date = user_data.get("last_used_date", "")
        daily_usage_count = user_data.get("daily_usage_count", 0)
        
        # 日付が変わった場合はカウントをリセット
        if last_used_date != today:
            user_data["last_used_date"] = today
            user_data["daily_usage_count"] = 1
        else:
            # 同じ日の場合は使用回数を増加
            user_data["daily_usage_count"] = daily_usage_count + 1
        
        return True, None
    
    # 無料ユーザーの制限チェック
    last_used_date = user_data.get("last_used_date", "")
    daily_usage_count = user_data.get("daily_usage_count", 0)
    
    # 日付が変わった場合はカウントをリセット
    if last_used_date != today:
        user_data["last_used_date"] = today
        user_data["daily_usage_count"] = 1
        return True, None
    
    # 同じ日の場合は制限チェック
    if daily_usage_count >= FREE_USER_DAILY_LIMIT:
        return False, f"😅 今日の分の利用回数を使い切っちゃいました！\n無料プランでは1日{FREE_USER_DAILY_LIMIT}回まで利用できます。明日また遊びに来てくださいね！✨\n\n💎 **もっと使いたい場合は有料プランがおすすめです！**\n🤖 このBotのプロフィールを見ると、プレミアム会員の詳細と登録方法が載ってるよ〜"
    
    # 使用回数を増加
    user_data["daily_usage_count"] = daily_usage_count + 1
    return True, None

def make_praise_image(praise_text):
    """褒めメッセージ画像を生成する"""
    try:
        logger.info(f"画像生成開始: テキスト='{praise_text}'")
        
        # 画像のサイズを指定
        width = 1080
        height = 1520
        
        # 画像の背景色を指定
        background_color = (255, 255, 255)
        
        # 画像を生成
        image = Image.new("RGB", (width, height), background_color)
        logger.info("ベース画像作成完了")
        
        # images_homehomeフォルダの中のjpgファイル一覧を取得
        images_dir = script_dir / "images_homehome"
        logger.info(f"画像フォルダパス: {images_dir}")
        
        if images_dir.exists():
            files = [f for f in os.listdir(images_dir) if f.endswith('.jpg')]
            logger.info(f"見つかった画像ファイル数: {len(files)}")
            
            if files:
                # ランダムに1つ選ぶ
                file = random.choice(files)
                logger.info(f"選択された画像: {file}")
                
                # 画像を開く
                img_path = images_dir / file
                logger.info(f"画像パス: {img_path}")
                img = Image.open(img_path)
                
                # imageに貼り付ける
                image.paste(img, (0, 0))
                logger.info("背景画像貼り付け完了")
            else:
                logger.warning("jpg画像が見つかりませんでした")
        else:
            logger.error(f"画像フォルダが存在しません: {images_dir}")
        
        # フォントを設定（システムフォントを使用）
        try:
            # Macの場合 - より安全なフォントを使用
            font = ImageFont.truetype("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", 30)
            logger.info("ヒラギノフォント読み込み成功")
        except Exception as e:
            logger.warning(f"ヒラギノフォント読み込み失敗: {e}")
            try:
                # Macの別のフォント
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 30)
                logger.info("Helveticaフォント読み込み成功")
            except Exception as e:
                logger.warning(f"Helveticaフォント読み込み失敗: {e}")
                try:
                    # Windowsの場合
                    font = ImageFont.truetype("C:/Windows/Fonts/msgothic.ttc", 30)
                    logger.info("MSゴシックフォント読み込み成功")
                except Exception as e:
                    logger.warning(f"MSゴシックフォント読み込み失敗: {e}")
                    # デフォルトフォント
                    font = ImageFont.load_default()
                    logger.info("デフォルトフォント使用")
        
        # テキストを処理（絵文字や特殊文字を除去）
        # 絵文字と特殊文字を除去し、ひらがな、カタカナ、漢字、英数字、基本記号のみ残す
        original_text = praise_text
        text = re.sub(r'[^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FAF\u0021-\u007E]', '', praise_text)
        text = text.replace("。", "").replace("、", "").replace(" ", "").replace("ー", "┃").replace("\n", "")
        logger.info(f"テキスト処理: '{original_text}' → '{text}'")
        
        # 36文字以内に調整
        if len(text) > 36:
            text = text[:36]
            logger.info(f"36文字に短縮: '{text}'")
        
        # 9文字ずつ4行に分割
        lines = []
        for i in range(0, min(len(text), 36), 9):
            lines.append(text[i:i+9])
        
        # 4行に満たない場合は空行を追加
        while len(lines) < 4:
            lines.append("")
        
        logger.info(f"分割された行: {lines}")
        
        # 各行を縦書きに変換
        vertical_lines = []
        for line in lines:
            vertical_lines.append("\n".join(list(line)))
        
        # テキストを画像に描画
        draw = ImageDraw.Draw(image)
        
        start_x = 855
        start_y = 415
        font_size = 30
        font_offset = 4
        
        # 行数が少ない場合のオフセット調整
        start_x -= (font_size + font_offset) * (4 - len([line for line in lines if line])) // 2
        
        # 各行を縦書きで描画
        for i, vertical_line in enumerate(vertical_lines):
            x_pos = start_x - (font_size + font_offset) * i
            draw.text((x_pos, start_y), vertical_line, font=font, fill=(0, 0, 0))
            logger.info(f"行{i+1}描画完了: x={x_pos}, テキスト='{vertical_line.replace(chr(10), '')}'")
        
        # 一時ファイルとして保存
        temp_path = script_dir / "temp_praise_image.jpg"
        image.save(temp_path)
        logger.info(f"画像保存完了: {temp_path}")
        
        return str(temp_path)
        
    except Exception as e:
        logger.error(f"画像生成エラー: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def extract_embed_content(message):
    """メッセージのEmbedから内容を抽出する"""
    try:
        if not message.embeds:
            return None
        
        embed_content = ""
        
        for embed in message.embeds:
            # タイトルを追加
            if embed.title:
                embed_content += f"# {embed.title}\n\n"
            
            # 説明文を追加
            if embed.description:
                embed_content += f"{embed.description}\n\n"
            
            # フィールドを追加
            for field in embed.fields:
                if field.name and field.value:
                    # リンク形式の場合は実際のテキストを抽出
                    field_value = field.value
                    # [テキスト](URL) 形式からテキスト部分を抽出
                    import re
                    link_match = re.search(r'\[([^\]]+)\]\([^)]+\)', field_value)
                    if link_match:
                        field_value = link_match.group(1)
                    
                    embed_content += f"**{field.name}**: {field_value}\n\n"
        
        if embed_content.strip():
            logger.info(f"Embed内容を抽出: {len(embed_content)}文字")
            return embed_content.strip()
        
        return None
        
    except Exception as e:
        logger.error(f"Embed内容抽出エラー: {e}")
        return None

async def read_text_attachment(attachment):
    """添付ファイルからテキスト内容を読み取る"""
    try:
        # テキストファイルの拡張子をチェック
        text_extensions = ['.txt', '.md', '.json', '.csv', '.log', '.py', '.js', '.html', '.css', '.xml']
        file_extension = Path(attachment.filename).suffix.lower()
        
        if file_extension not in text_extensions:
            return None
        
        # ファイルサイズをチェック（1MB以下）
        if attachment.size > 1024 * 1024:
            logger.warning(f"ファイルサイズが大きすぎます: {attachment.filename} ({attachment.size} bytes)")
            return None
        
        # ファイルをダウンロードして内容を読み取り
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment.url) as response:
                if response.status == 200:
                    content_bytes = await response.read()
                    # UTF-8で読み取り、失敗したら他のエンコーディングを試す
                    try:
                        content = content_bytes.decode('utf-8')
                        logger.info(f"テキストファイル読み取り成功: {attachment.filename} ({len(content)}文字)")
                        return content
                    except UnicodeDecodeError:
                        try:
                            content = content_bytes.decode('shift_jis')
                            logger.info(f"テキストファイル読み取り成功(Shift-JIS): {attachment.filename} ({len(content)}文字)")
                            return content
                        except UnicodeDecodeError:
                            logger.warning(f"テキストファイルのエンコーディングを判定できませんでした: {attachment.filename}")
                            return None
                else:
                    logger.warning(f"ファイルダウンロードに失敗: {attachment.filename} (status: {response.status})")
                    return None
                    
    except Exception as e:
        logger.error(f"テキストファイル読み取りエラー: {attachment.filename}, {e}")
        return None

def shorten_url(long_url):
    """is.gdを使ってURLを短縮する"""
    try:
        logger.info(f"URL短縮開始 - 元のURL長: {len(long_url)}文字")
        
        # is.gd APIを使用（POSTでリクエスト）
        api_url = "https://is.gd/create.php"
        data = {
            'format': 'simple',
            'url': long_url
        }
        
        response = requests.post(api_url, data=data, timeout=10)
        logger.info(f"is.gd応答ステータス: {response.status_code}")
        
        if response.status_code == 200:
            short_url = response.text.strip()
            # エラーメッセージの場合は失敗扱い
            if short_url.startswith('Error:') or not short_url.startswith('http'):
                logger.warning(f"is.gd短縮失敗 - エラー: {short_url}")
                return long_url  # 短縮失敗時は元のURLを返す
            
            logger.info(f"短縮成功: {short_url}")
            return short_url
        else:
            logger.warning(f"is.gd短縮失敗 - ステータス: {response.status_code}")
            return long_url  # 短縮失敗時は元のURLを返す
    except requests.exceptions.Timeout:
        logger.warning("URL短縮タイムアウト")
        return long_url
    except requests.exceptions.RequestException as e:
        logger.error(f"URL短縮接続エラー: {e}")
        return long_url
    except Exception as e:
        logger.error(f"URL短縮予期しないエラー: {e}")
        return long_url

async def transcribe_audio(message, channel, reaction_user):
    """音声ファイルを文字起こしする"""
    try:
        
        # 音声・動画ファイルを検索
        AUDIO_EXTS = ('.mp3', '.m4a', '.ogg', '.webm', '.wav')
        VIDEO_EXTS = ('.mp4',)
        target_attachment = None
        is_video = False
        
        for attachment in message.attachments:
            filename_lower = attachment.filename.lower()
            if filename_lower.endswith(AUDIO_EXTS):
                target_attachment = attachment
                is_video = False
                break
            elif filename_lower.endswith(VIDEO_EXTS):
                target_attachment = attachment
                is_video = True
                break
        
        if not target_attachment:
            await channel.send("⚠️ 音声・動画ファイルが見つかりません。対応形式: mp3, m4a, ogg, webm, wav, mp4")
            return
        
        # ファイルサイズチェック（音声：100MB、動画：500MB制限）
        if is_video:
            max_size = 500 * 1024 * 1024  # 500MB
            size_text = "500MB"
        else:
            max_size = 100 * 1024 * 1024   # 100MB
            size_text = "100MB"
        
        if target_attachment.size > max_size:
            await channel.send(f"❌ ファイルサイズが{size_text}を超えています。")
            return
        
        # メッセージリンクを作成
        message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
        
        if is_video:
            await channel.send(f"{reaction_user.mention} 🎬 動画から音声を抽出して文字起こしを開始するよ〜！ちょっと待っててね\n📎 元メッセージ: {message_link}")
        else:
            await channel.send(f"{reaction_user.mention} 🎤 音声の文字起こしを開始するよ〜！ちょっと待っててね\n📎 元メッセージ: {message_link}")
        
        # 一時ディレクトリ作成
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # ファイルをダウンロード
            file_extension = target_attachment.filename.split('.')[-1]
            original_file_path = temp_path / f"original.{file_extension}"
            await target_attachment.save(original_file_path)
            
            logger.info(f"ファイルダウンロード完了: {target_attachment.filename} ({target_attachment.size} bytes)")
            
            # 動画の場合は音声を抽出
            if is_video:
                try:
                    logger.info("動画から音声を抽出中...")
                    video = AudioSegment.from_file(original_file_path)
                    audio_file_path = temp_path / "extracted_audio.mp3"
                    video.export(audio_file_path, format="mp3")
                    logger.info("音声抽出完了")
                except Exception as e:
                    logger.error(f"音声抽出エラー: {e}")
                    await channel.send("❌ 動画から音声の抽出に失敗しました。")
                    return
            else:
                audio_file_path = original_file_path
            
            logger.info(f"処理対象ファイル: {audio_file_path}")
            
            # 音声ファイルを読み込み
            try:
                audio = AudioSegment.from_file(audio_file_path)
            except Exception as e:
                logger.error(f"音声ファイル読み込みエラー: {e}")
                await channel.send("❌ 音声ファイルの読み込みに失敗しました。対応形式か確認してください。")
                return
            
            # 音声の長さを確認し、分割処理を決定
            audio_length_ms = len(audio)
            audio_length_sec = audio_length_ms / 1000
            logger.info(f"音声長: {audio_length_sec:.2f}秒")
            
            # ファイルサイズに基づいて分割数を計算
            # 25MB制限を考慮して安全に20MBを目標とする
            target_size_mb = 20
            
            # 動画の場合は抽出されたMP3のサイズを使用、音声の場合は元ファイルサイズを使用
            if is_video:
                actual_size_mb = audio_file_path.stat().st_size / (1024 * 1024)
                logger.info(f"動画から抽出されたMP3サイズ: {actual_size_mb:.1f}MB")
            else:
                actual_size_mb = target_attachment.size / (1024 * 1024)
                logger.info(f"音声ファイルサイズ: {actual_size_mb:.1f}MB")
            
            time_based_split_count = max(1, int(audio_length_ms // (600 * 1000)))  # 10分基準
            size_based_split_count = max(1, int(actual_size_mb / target_size_mb))  # 実際のサイズ基準
            
            # より大きい分割数を採用（安全のため）
            split_count = max(time_based_split_count, size_based_split_count)
            logger.info(f"時間基準: {time_based_split_count}分割, サイズ基準: {size_based_split_count}分割 → {split_count}分割で処理します")
            
            # 音声ファイルを分割
            parts = []
            part_duration = audio_length_ms // split_count
            
            for i in range(split_count):
                start_time = i * part_duration
                end_time = min((i + 1) * part_duration, audio_length_ms)
                part_audio = audio[start_time:end_time]
                part_file_path = temp_path / f"part_{i}.mp3"
                part_audio.export(part_file_path, format="mp3")
                
                # 分割ファイルのサイズをチェック
                part_size_mb = part_file_path.stat().st_size / (1024 * 1024)
                parts.append(part_file_path)
                logger.info(f"分割ファイル作成: part_{i}.mp3 ({start_time}ms～{end_time}ms, {part_size_mb:.1f}MB)")
            
            # Whisperで各分割ファイルを文字起こし
            logger.info("Whisperによる文字起こし開始")
            full_transcription = ""
            
            for idx, part_file_path in enumerate(parts):
                logger.info(f"{idx+1}/{split_count}: {part_file_path.name} 文字起こし中...")
                
                try:
                    with open(part_file_path, "rb") as audio_file:
                        transcription = client_openai.audio.transcriptions.create(
                            model="whisper-1",
                            file=audio_file,
                            language="ja"  # 日本語指定
                        )
                        full_transcription += transcription.text + "\n"
                        logger.info(f"パート {idx+1} の文字起こし完了")
                except Exception as api_error:
                    logger.error(f"Whisper API エラー (パート {idx+1}): {api_error}")
                    # タイムアウトエラーの場合は特別なメッセージ
                    if "timeout" in str(api_error).lower() or "timed out" in str(api_error).lower():
                        await channel.send(f"{reaction_user.mention} ⏰ 申し訳ありません！文字起こし処理がタイムアウトしました。\n音声ファイルが大きいか、OpenAI APIが混雑している可能性があります。\n🔄 少し時間をおいてもう一度試してみてください。")
                    else:
                        await channel.send(f"{reaction_user.mention} ❌ 文字起こし処理中にエラーが発生しました。\n🔄 もう一度試してみてください。")
                    return
            
            logger.info(f"文字起こし完了: {len(full_transcription)}文字")
            
            # 文字起こし結果をテキストファイルとして保存
            original_name = os.path.splitext(target_attachment.filename)[0]
            transcript_filename = f"{original_name}_transcript.txt"
            transcript_path = temp_path / transcript_filename
            
            with open(transcript_path, 'w', encoding='utf-8') as f:
                if is_video:
                    f.write(f"動画ファイル: {target_attachment.filename}\n")
                else:
                    f.write(f"音声ファイル: {target_attachment.filename}\n")
                f.write(f"音声長: {audio_length_sec:.2f}秒\n")
                f.write(f"処理日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("-" * 50 + "\n\n")
                f.write(full_transcription)
            
            # 結果をDiscordに分割送信（1000文字ずつ）
            await channel.send("🎉 文字起こしが完了したよ〜！")
            await channel.send("-" * 30)
            
            if full_transcription.strip():
                # 1000文字ずつに分割して送信
                for chunk in [full_transcription[j:j+1000] for j in range(0, len(full_transcription), 1000)]:
                    await channel.send(chunk)
                    await asyncio.sleep(1)  # 連続送信を避けるためのウェイト
            else:
                await channel.send("⚠️ 文字起こし結果が空でした。")
            
            await channel.send("-" * 30)
            file_message = await channel.send("📄 文字起こし結果のテキストファイルです！", file=discord.File(transcript_path))
            
            # 文字起こし結果ファイルに自動でリアクションを追加
            reactions = ['👍', '❓', '❤️', '✏️', '📝']
            for reaction in reactions:
                try:
                    await file_message.add_reaction(reaction)
                    await asyncio.sleep(0.5)  # Discord API レート制限対策
                except Exception as e:
                    logger.warning(f"リアクション追加エラー ({reaction}): {e}")
            
            logger.info("文字起こし結果ファイルにリアクションを追加しました")
            
    except Exception as e:
        logger.error(f"音声文字起こしエラー: {e}")
        await channel.send("❌ 文字起こし処理中にエラーが発生しました。")

@bot.event
async def on_ready():
    """Bot起動時の処理"""
    print(f'{bot.user} にログインしました')
    
    # 登録されているコマンドを確認
    print(f"登録されているコマンド数: {len(bot.tree.get_commands())}")
    for cmd in bot.tree.get_commands():
        print(f"- {cmd.name}: {cmd.description}")
    
    # スラッシュコマンドを強制的に書き換え
    try:
        test_guild = discord.Object(id=TEST_GUILD_ID)
        
        # Step 1: 既存のギルドコマンドを完全にクリア
        print("=== 既存コマンドのクリア処理開始 ===")
        bot.tree.clear_commands(guild=test_guild)
        empty_sync = await bot.tree.sync(guild=test_guild)
        print(f"テストサーバーのコマンドをクリア完了: {len(empty_sync)} 個")
        
        # Step 2: 新しいコマンドを追加
        print("=== 新しいコマンドの追加処理開始 ===")
        synced_guild = await bot.tree.sync(guild=test_guild)
        print(f'テストサーバー ({TEST_GUILD_ID}) に {len(synced_guild)} 個のスラッシュコマンドを強制同期しました')
        for cmd in synced_guild:
            print(f"  ✅ {cmd['name']}: {cmd.get('description', 'N/A')}")
        
        # Step 3: グローバルにも同期
        print("=== グローバル同期処理開始 ===")
        synced_global = await bot.tree.sync()
        print(f'グローバルに {len(synced_global)} 個のスラッシュコマンドを同期しました')
        
        print("=== コマンド同期処理完了 ===")
        
    except Exception as e:
        logger.error(f'❌ スラッシュコマンドの同期に失敗しました: {e}')
        import traceback
        logger.error(traceback.format_exc())

@bot.tree.command(name="help", description="利用可能なコマンド一覧を表示します")
async def help_command(interaction: discord.Interaction):
    """ヘルプコマンド"""
    embed = discord.Embed(
        title="🤖 Bot コマンド一覧",
        description="利用可能なコマンド:",
        color=0x00ff00
    )
    
    embed.add_field(
        name="/help", 
        value="このヘルプメッセージを表示", 
        inline=False
    )
    embed.add_field(
        name="/activate", 
        value="このチャンネルでBotを有効化（管理者のみ）", 
        inline=False
    )
    embed.add_field(
        name="/deactivate", 
        value="このチャンネルでBotを無効化（管理者のみ）", 
        inline=False
    )
    embed.add_field(
        name="/status", 
        value="サーバー内の有効チャンネル一覧を表示（管理者のみ）", 
        inline=False
    )
    embed.add_field(
        name="/set_custom_prompt_x_post", 
        value="X投稿用のカスタムプロンプトを設定（空白入力で無効化）", 
        inline=False
    )
    embed.add_field(
        name="/set_custom_prompt_article", 
        value="記事作成用のカスタムプロンプトを設定（空白入力で無効化）", 
        inline=False
    )
    embed.add_field(
        name="/set_custom_prompt_memo", 
        value="メモ作成用のカスタムプロンプトを設定（空白入力で無効化）", 
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)

# カスタムプロンプト設定用のModalクラス
class CustomPromptModal(discord.ui.Modal, title='X投稿用カスタムプロンプト設定'):
    def __init__(self, current_prompt=""):
        super().__init__()
        # テキスト入力エリア（複数行対応）
        self.prompt_input = discord.ui.TextInput(
            label='カスタムプロンプト',
            placeholder='X投稿生成用のプロンプトを入力してください...\n改行も使用できます。\n\n※ 空のまま送信するとカスタムプロンプトが無効になり、デフォルトプロンプトが使用されます。',
            style=discord.TextStyle.paragraph,  # 複数行入力
            max_length=2000,
            required=False,
            default=current_prompt  # 既存の値をプリフィル
        )
        self.add_item(self.prompt_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            prompt = self.prompt_input.value.strip()  # 前後の空白を削除
            
            # ユーザーデータを読み込み（存在しない場合は新規作成）
            user_id = interaction.user.id
            user_data = load_user_data(user_id)
            if user_data is None:
                user_data = {
                    "custom_prompt_x_post": "",
                    "status": "free",
                    "last_used_date": "",
                    "daily_usage_count": 0
                }
            
            # カスタムプロンプトを更新
            user_data["custom_prompt_x_post"] = prompt
            
            # ユーザーデータを保存
            save_user_data(user_id, user_data)
            
            # 設定内容に応じてメッセージを変更
            if prompt:
                print(f"ユーザー {interaction.user.name} ({user_id}) がカスタムプロンプトを設定しました")
                print(f"プロンプト内容: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")
                await interaction.response.send_message("✅ カスタムプロンプトを設定しました！", ephemeral=True)
            else:
                print(f"ユーザー {interaction.user.name} ({user_id}) がカスタムプロンプトを無効化しました")
                await interaction.response.send_message("✅ カスタムプロンプトを無効化しました。デフォルトプロンプトを使用します。", ephemeral=True)
            
        except Exception as e:
            logger.error(f"カスタムプロンプト設定エラー: {e}")
            await interaction.response.send_message("❌ エラーが発生しました。管理者にお問い合わせください。", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"Modal エラー: {error}")
        await interaction.response.send_message("❌ エラーが発生しました。管理者にお問い合わせください。", ephemeral=True)

@bot.tree.command(name="set_custom_prompt_x_post", description="X投稿用のカスタムプロンプトを設定します")
async def set_custom_prompt_x_post_command(interaction: discord.Interaction):
    """カスタムプロンプト設定コマンド"""
    # 既存のユーザーデータを読み込み
    user_id = interaction.user.id
    user_data = load_user_data(user_id)
    current_prompt = ""
    if user_data and "custom_prompt_x_post" in user_data:
        current_prompt = user_data["custom_prompt_x_post"]
    
    modal = CustomPromptModal(current_prompt)
    await interaction.response.send_modal(modal)

# 記事作成用カスタムプロンプト設定のModalクラス
class CustomArticlePromptModal(discord.ui.Modal, title='記事作成用カスタムプロンプト設定'):
    def __init__(self, current_prompt=""):
        super().__init__()
        # テキスト入力エリア（複数行対応）
        self.prompt_input = discord.ui.TextInput(
            label='カスタムプロンプト',
            placeholder='記事作成用のプロンプトを入力してください...\n改行も使用できます。\n\n※ 空のまま送信するとカスタムプロンプトが無効になり、デフォルトプロンプトが使用されます。',
            style=discord.TextStyle.paragraph,  # 複数行入力
            max_length=2000,
            required=False,
            default=current_prompt  # 既存の値をプリフィル
        )
        self.add_item(self.prompt_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            prompt = self.prompt_input.value.strip()  # 前後の空白を削除
            
            # ユーザーデータを読み込み（存在しない場合は新規作成）
            user_id = interaction.user.id
            user_data = load_user_data(user_id)
            if user_data is None:
                user_data = {
                    "custom_prompt_x_post": "",
                    "custom_prompt_article": "",
                    "custom_prompt_memo": "",
                    "status": "free",
                    "last_used_date": "",
                    "daily_usage_count": 0
                }
            
            # 記事用カスタムプロンプトを更新
            user_data["custom_prompt_article"] = prompt
            
            # ユーザーデータを保存
            save_user_data(user_id, user_data)
            
            # 設定内容に応じてメッセージを変更
            if prompt:
                print(f"ユーザー {interaction.user.name} ({user_id}) が記事用カスタムプロンプトを設定しました")
                print(f"プロンプト内容: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")
                await interaction.response.send_message("✅ 記事作成用カスタムプロンプトを設定しました！", ephemeral=True)
            else:
                print(f"ユーザー {interaction.user.name} ({user_id}) が記事用カスタムプロンプトを無効化しました")
                await interaction.response.send_message("✅ 記事作成用カスタムプロンプトを無効化しました。デフォルトプロンプトを使用します。", ephemeral=True)
            
        except Exception as e:
            logger.error(f"記事用カスタムプロンプト設定エラー: {e}")
            await interaction.response.send_message("❌ エラーが発生しました。管理者にお問い合わせください。", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"Modal エラー: {error}")
        await interaction.response.send_message("❌ エラーが発生しました。管理者にお問い合わせください。", ephemeral=True)

@bot.tree.command(name="set_custom_prompt_article", description="記事作成用のカスタムプロンプトを設定します")
async def set_custom_prompt_article_command(interaction: discord.Interaction):
    """記事用カスタムプロンプト設定コマンド"""
    # 既存のユーザーデータを読み込み
    user_id = interaction.user.id
    user_data = load_user_data(user_id)
    current_prompt = ""
    if user_data and "custom_prompt_article" in user_data:
        current_prompt = user_data["custom_prompt_article"]
    
    modal = CustomArticlePromptModal(current_prompt)
    await interaction.response.send_modal(modal)

# メモ作成用カスタムプロンプト設定のModalクラス
class CustomMemoPromptModal(discord.ui.Modal, title='メモ作成用カスタムプロンプト設定'):
    def __init__(self):
        super().__init__()

    # テキスト入力エリア（複数行対応）
    prompt_input = discord.ui.TextInput(
        label='カスタムプロンプト',
        placeholder='メモ作成用のプロンプトを入力してください...\n改行も使用できます。\n\n※ 空のまま送信するとカスタムプロンプトが無効になり、デフォルトプロンプトが使用されます。',
        style=discord.TextStyle.paragraph,  # 複数行入力
        max_length=2000,
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            prompt = self.prompt_input.value.strip()  # 前後の空白を削除
            
            # ユーザーデータを読み込み（存在しない場合は新規作成）
            user_id = interaction.user.id
            user_data = load_user_data(user_id)
            if user_data is None:
                user_data = {
                    "custom_prompt_x_post": "",
                    "custom_prompt_article": "",
                    "custom_prompt_memo": "",
                    "status": "free",
                    "last_used_date": "",
                    "daily_usage_count": 0
                }
            
            # メモ用カスタムプロンプトを更新
            user_data["custom_prompt_memo"] = prompt
            
            # ユーザーデータを保存
            save_user_data(user_id, user_data)
            
            # 設定内容に応じてメッセージを変更
            if prompt:
                print(f"ユーザー {interaction.user.name} ({user_id}) がメモ用カスタムプロンプトを設定しました")
                print(f"プロンプト内容: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")
                await interaction.response.send_message("✅ メモ作成用カスタムプロンプトを設定しました！", ephemeral=True)
            else:
                print(f"ユーザー {interaction.user.name} ({user_id}) がメモ用カスタムプロンプトを無効化しました")
                await interaction.response.send_message("✅ メモ作成用カスタムプロンプトを無効化しました。デフォルトプロンプトを使用します。", ephemeral=True)
            
        except Exception as e:
            logger.error(f"メモ用カスタムプロンプト設定エラー: {e}")
            await interaction.response.send_message("❌ エラーが発生しました。管理者にお問い合わせください。", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"Modal エラー: {error}")
        await interaction.response.send_message("❌ エラーが発生しました。管理者にお問い合わせください。", ephemeral=True)

@bot.tree.command(name="set_custom_prompt_memo", description="メモ作成用のカスタムプロンプトを設定します")
async def set_custom_prompt_memo_command(interaction: discord.Interaction):
    """メモ用カスタムプロンプト設定コマンド"""
    # 既存のユーザーデータを読み込み
    user_id = interaction.user.id
    user_data = load_user_data(user_id)
    current_prompt = ""
    if user_data and "custom_prompt_memo" in user_data:
        current_prompt = user_data["custom_prompt_memo"]
    
    modal = CustomMemoPromptModal(current_prompt)
    await interaction.response.send_modal(modal)

@bot.tree.command(name="activate", description="このチャンネルでBotを有効化します")
async def activate_command(interaction: discord.Interaction):
    """アクティベートコマンド"""
    # 管理者権限チェック
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用できます。", ephemeral=True)
        return
    
    server_id = str(interaction.guild.id)
    channel_id = str(interaction.channel.id)
    
    # サーバーデータを読み込み
    server_data = load_server_data(server_id)
    if server_data is None:
        server_data = {
            "server_id": server_id,
            "server_name": interaction.guild.name,
            "active_channel_ids": []
        }
    
    # server_nameを更新（サーバー名が変更された場合に対応）
    server_data['server_name'] = interaction.guild.name
    
    # チャンネルIDを追加
    if channel_id not in server_data['active_channel_ids']:
        server_data['active_channel_ids'].append(channel_id)
        save_server_data(server_id, server_data)
        
        # 使い方ガイドメッセージを作成
        guide_message = (
            f"✅ このチャンネル（{interaction.channel.name}）でBotを有効化しました！\n\n"
            "**📖 使い方**\n"
            "メッセージに以下のリアクションを付けると、それぞれの機能が動作します：\n\n"
            "👍 **X投稿生成** - メッセージをX（旧Twitter）投稿用に最適化\n"
            "🎤 **音声文字起こし** - 音声ファイルをテキストに変換\n"
            "❓ **AI解説** - メッセージ内容を詳しく解説\n"
            "❤️ **褒めメッセージ** - 熱烈な応援メッセージと画像を生成\n"
            "✏️ **メモ作成** - Obsidian用のMarkdownメモを自動生成\n"
            "📝 **記事作成** - 記事を作成（カスタムプロンプト対応）\n\n"
            "👇試しに下のリアクションを押してみて👇"
        )
        
        await interaction.response.send_message(guide_message)
        
        # 送信したメッセージを取得してリアクションを追加
        message = await interaction.original_response()
        reactions = ['👍', '❓', '❤️', '✏️', '📝']
        for emoji in reactions:
            await message.add_reaction(emoji)
            await asyncio.sleep(0.5)  # リアクション追加の間隔を空ける
        
        # サンプル音声ファイルを送信
        sample_audio_path = script_dir / "audio" / "sample_voice.mp3"
        if sample_audio_path.exists():
            try:
                audio_message = await interaction.followup.send(
                    "🎵 試しに音声文字起こし機能を使ってみてください！",
                    file=discord.File(sample_audio_path)
                )
                # サンプル音声にマイクリアクションを追加
                await audio_message.add_reaction('🎤')
                logger.info("サンプル音声ファイル送信完了")
            except Exception as e:
                logger.error(f"サンプル音声ファイル送信エラー: {e}")
        else:
            logger.warning(f"サンプル音声ファイルが見つかりません: {sample_audio_path}")
    else:
        await interaction.response.send_message(f"ℹ️ このチャンネル（{interaction.channel.name}）は既に有効です。")

@bot.tree.command(name="deactivate", description="このチャンネルでBotを無効化します")
async def deactivate_command(interaction: discord.Interaction):
    """ディアクティベートコマンド"""
    # 管理者権限チェック
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用できます。", ephemeral=True)
        return
    
    server_id = str(interaction.guild.id)
    channel_id = str(interaction.channel.id)
    
    # サーバーデータを読み込み
    server_data = load_server_data(server_id)
    if server_data is None:
        await interaction.response.send_message("❌ サーバーデータが見つかりません。")
        return
    
    # チャンネルIDを削除
    if channel_id in server_data['active_channel_ids']:
        server_data['active_channel_ids'].remove(channel_id)
        save_server_data(server_id, server_data)
        await interaction.response.send_message(f"✅ このチャンネル（{interaction.channel.name}）でBotを無効化しました。")
    else:
        await interaction.response.send_message(f"ℹ️ このチャンネル（{interaction.channel.name}）は既に無効です。")

@bot.tree.command(name="status", description="サーバー内の有効チャンネル一覧を表示します")
async def status_command(interaction: discord.Interaction):
    """ステータスコマンド"""
    # 管理者権限チェック
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ このコマンドは管理者のみ使用できます。", ephemeral=True)
        return
    
    server_id = str(interaction.guild.id)
    server_data = load_server_data(server_id)
    
    if server_data and "active_channel_ids" in server_data:
        channel_list = []
        for channel_id in server_data["active_channel_ids"]:
            channel = bot.get_channel(int(channel_id))
            if channel:
                channel_list.append(f"• {channel.name}")
            else:
                channel_list.append(f"• ID: {channel_id} (チャンネルが見つかりません)")
        
        if channel_list:
            channel_text = "\n".join(channel_list)
        else:
            channel_text = "有効なチャンネルがありません"
    else:
        channel_text = "有効なチャンネルがありません"
    
    embed = discord.Embed(
        title="📋 有効チャンネル一覧",
        description=channel_text,
        color=0x00ff00
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="stats", description="Bot統計情報を表示します")
async def stats_command(interaction: discord.Interaction):
    """統計コマンド（オーナー専用）"""
    # オーナー権限チェック
    user_id = str(interaction.user.id)
    
    # settings.jsonからowner_user_idを取得
    settings_path = script_dir / "settings.json"
    if settings_path.exists():
        with open(settings_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
            owner_user_id = settings.get("owner_user_id")
    else:
        owner_user_id = None
    
    # オーナーかどうかチェック
    if not owner_user_id or user_id != str(owner_user_id):
        await interaction.response.send_message("❌ このコマンドはオーナーのみ使用できます。", ephemeral=True)
        return
    
    try:
        # 統計を計算
        stats = stats_manager.get_stats_summary()
        server_count = len(bot.guilds)
        
        embed = discord.Embed(
            title="📊 Bot統計情報",
            color=0x00ff00
        )
        
        embed.add_field(name="📅 集計日", value=stats["date"], inline=True)
        embed.add_field(name="🏠 現在のサーバー数", value=f"{server_count:,}", inline=True)
        embed.add_field(name="🏠 記録時サーバー数", value=f"{stats['server_count']:,}", inline=True)
        embed.add_field(name="📈 DAU", value=f"{stats['dau']:,}", inline=True)
        embed.add_field(name="📊 MAU", value=f"{stats['mau']:,}", inline=True)
        embed.add_field(name="⚡ 今日のアクション数", value=f"{stats['total_actions_today']:,}", inline=True)
        embed.add_field(name="🕐 更新時刻", value=datetime.now().strftime("%H:%M:%S"), inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
    except Exception as e:
        logger.error(f"統計コマンドエラー: {e}")
        await interaction.response.send_message("❌ 統計取得中にエラーが発生しました。", ephemeral=True)

@bot.tree.command(name="restart", description="Botを再起動します（オーナー専用）")
async def restart_command(interaction: discord.Interaction):
    """Botリスタートコマンド（オーナー専用）"""
    # オーナー権限チェック
    user_id = str(interaction.user.id)
    
    # settings.jsonからowner_user_idを取得
    settings_path = script_dir / "settings.json"
    if settings_path.exists():
        with open(settings_path, 'r', encoding='utf-8') as f:
            settings = json.load(f)
            owner_user_id = settings.get("owner_user_id")
    else:
        owner_user_id = None
    
    # オーナーかどうかチェック
    if not owner_user_id or user_id != str(owner_user_id):
        await interaction.response.send_message("❌ このコマンドはオーナーのみ使用できます。", ephemeral=True)
        return
    
    try:
        # 再起動メッセージを送信
        await interaction.response.send_message("🔄 Botを再起動しています...", ephemeral=True)
        
        # ログに記録
        logger.info(f"Bot再起動要求 - ユーザー: {interaction.user.name} ({user_id})")
        
        # Bot終了（プロセスマネージャーがあれば自動再起動、なければ手動再起動が必要）
        await bot.close()
        
    except Exception as e:
        logger.error(f"再起動コマンドエラー: {e}")
        await interaction.followup.send("❌ 再起動中にエラーが発生しました。", ephemeral=True)

@bot.event
async def on_raw_reaction_add(payload):
    """リアクション追加時の処理"""
    # Botのリアクションは無視
    if payload.user_id == bot.user.id:
        return
    
    # リアクションの種類をチェック
    if payload.emoji.name in ['👍', '🎤', '❤️', '❓', '✏️', '📝']:
        server_id = str(payload.guild_id)
        channel_id = str(payload.channel_id)
        
        # チャンネルが有効かチェック
        if is_channel_active(server_id, channel_id):
            # チャンネルとメッセージを取得
            channel = bot.get_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            user = await bot.fetch_user(payload.user_id)
            
            # 統計記録（ユーザーアクティビティ）
            await stats_manager.record_user_activity(str(payload.user_id), bot)
            
            logger.info(f"{payload.emoji.name} リアクションを検知しました！")
            logger.info(f"サーバー: {message.guild.name}")
            logger.info(f"チャンネル: {channel.name}")
            logger.info(f"ユーザー: {user.name if user else '不明'}")
            logger.info(f"メッセージ: {message.content if message.content else '(空のメッセージ)'}")
            logger.info("-" * 50)
            
            # 共通ユーザーデータ処理
            user_data = load_user_data(user.id)
            if user_data is None:
                # 新規ユーザー
                user_data = {
                    "user_id": str(user.id),
                    "username": user.name,
                    "custom_prompt_x_post": "",
                    "custom_prompt_article": "",
                    "custom_prompt_memo": "",
                    "status": "free",
                    "last_used_date": "",
                    "daily_usage_count": 0
                }
                save_user_data(user.id, user_data)
                logger.info(f"新規ユーザー {user.name} ({user.id}) のデータを作成しました")
            else:
                # 既存ユーザーのマイグレーション
                user_data, migration_needed = migrate_user_data(user_data, user.id, user.name)
                if migration_needed:
                    save_user_data(user.id, user_data)
                    logger.info(f"ユーザー {user.name} ({user.id}) のデータをマイグレーションしました")
            
            # プレミアム状態確認
            is_premium = await is_premium_user(user.id)
            
            # ユーザー情報とstatusを更新
            user_data["user_id"] = str(user.id)
            user_data["username"] = user.name
            user_data["status"] = "premium" if is_premium else "free"
            
            # 使用制限チェック
            can_use, limit_message = can_use_feature(user_data, is_premium)
            if not can_use:
                await channel.send(f"{user.mention} {limit_message}")
                return
            
            # 使用回数更新
            save_user_data(user.id, user_data)
            
            
            # 👍 サムズアップ：X投稿要約
            if payload.emoji.name == '👍':
                # メッセージ内容または添付ファイル、Embedからテキストを取得
                input_text = message.content
                
                # Embedがある場合は内容を抽出
                embed_content = extract_embed_content(message)
                if embed_content:
                    if input_text:
                        input_text += f"\n\n【Embed内容】\n{embed_content}"
                    else:
                        input_text = embed_content
                    logger.info("Embed内容を追加")
                
                # 添付ファイルがある場合、テキストファイルの内容を読み取り
                if message.attachments:
                    for attachment in message.attachments:
                        file_content = await read_text_attachment(attachment)
                        if file_content:
                            if input_text:
                                input_text += f"\n\n【ファイル: {attachment.filename}】\n{file_content}"
                            else:
                                input_text = f"【ファイル: {attachment.filename}】\n{file_content}"
                            logger.info(f"添付ファイルの内容を追加: {attachment.filename}")
                
                if input_text:
                    # URL検出・警告
                    await check_content_for_urls(input_text, user, channel)
                    
                    # モデルを選択
                    model = PREMIUM_USER_MODEL if is_premium else FREE_USER_MODEL
                    
                    # 処理開始メッセージを送信
                    message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
                    await channel.send(f"{user.mention} X用の投稿を作ってあげるね〜！ちょっと待っててね\n📎 元メッセージ: {message_link}")
                    
                    # X投稿用プロンプトを読み込み（カスタムプロンプトを優先）
                    x_prompt = None
                    
                    # 1. ユーザーのカスタムプロンプトをチェック
                    if user_data and user_data.get('custom_prompt_x_post'):
                        x_prompt = user_data['custom_prompt_x_post']
                        logger.info(f"ユーザー {user.name} のカスタムプロンプトを使用")
                    
                    # 2. カスタムプロンプトがない場合はデフォルトプロンプトファイルを使用
                    if not x_prompt:
                        prompt_path = script_dir / "prompt" / "x_post.txt"
                        if prompt_path.exists():
                            with open(prompt_path, 'r', encoding='utf-8') as f:
                                x_prompt = f.read()
                            logger.info("デフォルトプロンプトファイルを使用")
                        else:
                            x_prompt = "あなたはDiscordの投稿をX（旧Twitter）用に要約するアシスタントです。140文字以内で簡潔に要約してください。"
                            logger.info("フォールバックプロンプトを使用")
                    
                    # プロンプトにJSON出力指示と文字数制限を追加
                    x_prompt += "\n\n出力は以下のJSON形式で返してください：\n{\"content\": \"X投稿用のテキスト\"}\n\n重要な文字数制限：\n- プロンプトで300文字以下の文字数指定がある場合はその文字数に従ってください\n- プロンプトで300文字を超える文字数指定がある場合や指定がない場合は、必ず300文字以内で出力してください\n- 絶対に300文字を超えないでください"
                    
                    # OpenAI APIで要約を生成
                    if client_openai:
                        try:
                            response = client_openai.chat.completions.create(
                                model=model,
                                messages=[
                                    {"role": "system", "content": x_prompt},
                                    {"role": "user", "content": input_text}
                                ],
                                max_tokens=1000,
                                temperature=0.9,
                                response_format={"type": "json_object"}
                            )
                            
                            # JSONレスポンスをパース
                            response_content = response.choices[0].message.content
                            try:
                                response_json = json.loads(response_content)
                                summary = response_json.get("content", response_content)
                            except json.JSONDecodeError:
                                logger.warning(f"JSON解析エラー、生のレスポンスを使用: {response_content}")
                                summary = response_content
                            
                            # X投稿用のURLを生成
                            x_intent_url = f"https://twitter.com/intent/tweet?text={urllib.parse.quote(summary)}"
                            
                            # URLを短縮
                            shortened_url = shorten_url(x_intent_url)
                            
                            # 結果を送信（Discord制限に合わせて文字数制限）
                            # embed descriptionは4096文字制限、fieldは1024文字制限
                            display_summary = summary[:4000] + "..." if len(summary) > 4000 else summary
                            
                            embed = discord.Embed(
                                title="📝 X投稿用要約",
                                description=display_summary,
                                color=0x1DA1F2
                            )
                            
                            embed.add_field(
                                name="X投稿リンク👇",
                                value=f"[クリックして投稿]({shortened_url})",
                                inline=False
                            )
                            
                            # 完了メッセージと結果を送信
                            await channel.send("🎉 できたよ〜！Xに投稿する場合は下のリンクをクリックしてね！")
                            await channel.send(embed=embed)
                            
                        except Exception as e:
                            logger.error(f"OpenAI API エラー: {e}")
                            await channel.send(f"{user.mention} ❌ 要約の生成中にエラーが発生しました。")
                    else:
                        logger.error("エラー: OpenAI APIキーが設定されていません")
                        await channel.send(f"{user.mention} ❌ エラーが発生しました。管理者にお問い合わせください。")
                else:
                    await channel.send(f"{user.mention} ⚠️ **X投稿を作成するためにはテキストが必要です**\n\n"
                                     f"以下のいずれかを行ってから👍リアクションしてください：\n"
                                     f"• テキストメッセージを投稿する\n"
                                     f"• テキストファイル（.txt）を添付する\n"
                                     f"• 音声ファイルの場合は🎤で文字起こしをしてからそのファイルに👍する\n\n"
                                     f"音声ファイルのみでは直接X投稿は作成できません。")
            
            # 🎤 マイク：音声・動画文字起こし
            elif payload.emoji.name == '🎤':
                # 音声・動画ファイルがあるかチェック
                if message.attachments:
                    await transcribe_audio(message, channel, user)
                else:
                    await channel.send(f"{user.mention} ⚠️ **🎤は音声・動画の文字起こし専用です**\n\n"
                                     f"音声ファイル（mp3、wav、m4a等）または動画ファイル（mp4、mov等）が添付されたメッセージにリアクションしてください。\n\n"
                                     f"テキストのみのメッセージには🎤ではなく、以下のリアクションをお使いください：\n"
                                     f"• 👍 X投稿作成\n"
                                     f"• ❓ AI解説\n"
                                     f"• ❤️ 絶賛モード\n"
                                     f"• ✏️ 記事作成")
            
            # ❤️ ハート：絶賛モード
            elif payload.emoji.name == '❤️':
                # メッセージ内容または添付ファイル、Embedからテキストを取得
                input_text = message.content
                
                # Embedがある場合は内容を抽出
                embed_content = extract_embed_content(message)
                if embed_content:
                    if input_text:
                        input_text += f"\n\n【Embed内容】\n{embed_content}"
                    else:
                        input_text = embed_content
                    logger.info("Embed内容を追加")
                
                # 添付ファイルがある場合、テキストファイルの内容を読み取り
                if message.attachments:
                    for attachment in message.attachments:
                        file_content = await read_text_attachment(attachment)
                        if file_content:
                            if input_text:
                                input_text += f"\n\n【ファイル: {attachment.filename}】\n{file_content}"
                            else:
                                input_text = f"【ファイル: {attachment.filename}】\n{file_content}"
                            logger.info(f"添付ファイルの内容を追加: {attachment.filename}")
                
                if input_text:
                    # URL検出・警告
                    await check_content_for_urls(input_text, user, channel)
                    
                    # 処理開始メッセージを送信
                    message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
                    await channel.send(f"{user.mention} わー！褒めさせて〜！ちょっと待っててね✨\n📎 元メッセージ: {message_link}")
                    
                    # モデルを選択
                    model = PREMIUM_USER_MODEL if is_premium else FREE_USER_MODEL
                    
                    # 褒めプロンプトを読み込み
                    praise_prompt = None
                    prompt_path = script_dir / "prompt" / "heart_praise.txt"
                    if prompt_path.exists():
                        with open(prompt_path, 'r', encoding='utf-8') as f:
                            praise_prompt = f.read()
                        logger.info("褒めプロンプトファイルを使用")
                    else:
                        praise_prompt = "あなたはDiscordメッセージの内容について極めて熱烈に褒めまくるアシスタントです。どんな内容でも強烈に・熱烈に・感動的に褒めてください。ユーザーのモチベーション向上に特化した内容で、800文字以内で褒めてください。"
                        logger.info("フォールバック褒めプロンプトを使用")
                    
                    # OpenAI APIで褒めメッセージを生成（JSONモード）
                    if client_openai:
                        try:
                            response = client_openai.chat.completions.create(
                                model=model,
                                messages=[
                                    {"role": "system", "content": praise_prompt},
                                    {"role": "user", "content": input_text}
                                ],
                                max_tokens=1500,
                                temperature=0.9,
                                response_format={"type": "json_object"}
                            )
                            
                            # JSONレスポンスをパース
                            response_content = response.choices[0].message.content
                            try:
                                praise_json = json.loads(response_content)
                                long_praise = praise_json.get("long_praise", "")
                                short_praise = praise_json.get("short_praise", "")
                            except json.JSONDecodeError:
                                logger.warning(f"JSON解析エラー、フォールバックを使用: {response_content}")
                                long_praise = response_content[:400]
                                short_praise = response_content[:20]
                            
                            # 1. まず400字の激烈褒めをDiscordに投稿
                            if len(long_praise) > 400:
                                long_praise = long_praise[:400] + "..."
                            
                            await channel.send(long_praise)
                            
                            # 2. 25字の短文褒めで画像を生成
                            if len(short_praise) > 25:
                                short_praise = short_praise[:25]
                            
                            # 画像生成用テキスト処理（絵文字除去）
                            image_text = re.sub(r'[^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FAF\u0021-\u007E]', '', short_praise)
                            image_text = image_text.replace("。", "").replace("、", "").replace(" ", "").replace("\n", "")
                            
                            # 褒め画像を生成
                            image_path = make_praise_image(image_text)
                            
                            # 3. 画像を送信
                            if image_path and os.path.exists(image_path):
                                try:
                                    await channel.send("🎉 褒め画像をお作りしました！", file=discord.File(image_path))
                                    logger.info("褒め画像送信成功")
                                    # 一時ファイルを削除
                                    try:
                                        os.remove(image_path)
                                        logger.info("一時ファイル削除完了")
                                    except Exception as e:
                                        logger.warning(f"一時ファイル削除失敗: {e}")
                                except Exception as e:
                                    logger.error(f"画像送信エラー: {e}")
                                    await channel.send("※ 画像の生成に失敗しましたが、褒めメッセージは送れました！")
                            else:
                                logger.warning("画像パスが無効か、ファイルが存在しません")
                                await channel.send("※ 画像の生成に失敗しましたが、褒めメッセージは送れました！")
                            
                        except Exception as e:
                            logger.error(f"OpenAI API エラー (褒め機能): {e}")
                            await channel.send(f"{user.mention} ❌ 褒めメッセージの生成中にエラーが発生しました。")
                    else:
                        logger.error("エラー: OpenAI APIキーが設定されていません")
                        await channel.send(f"{user.mention} ❌ エラーが発生しました。管理者にお問い合わせください。")
                else:
                    await channel.send(f"{user.mention} ⚠️ **❤️褒めメッセージを作成するためにはテキストが必要です**\n\n"
                                     f"以下のいずれかを行ってから❤️リアクションしてください：\n"
                                     f"• テキストメッセージを投稿する\n"
                                     f"• テキストファイル（.txt）を添付する\n"
                                     f"• 音声ファイルの場合は🎤で文字起こしをしてからそのファイルに❤️する\n\n"
                                     f"あなたの投稿内容を元に素敵な褒めメッセージと画像を生成します！")
            
            # ❓ 疑問符：AI説明
            elif payload.emoji.name == '❓':
                # メッセージ内容または添付ファイル、Embedからテキストを取得
                input_text = message.content
                
                # Embedがある場合は内容を抽出
                embed_content = extract_embed_content(message)
                if embed_content:
                    if input_text:
                        input_text += f"\n\n【Embed内容】\n{embed_content}"
                    else:
                        input_text = embed_content
                    logger.info("Embed内容を追加")
                
                # 添付ファイルがある場合、テキストファイルの内容を読み取り
                if message.attachments:
                    for attachment in message.attachments:
                        file_content = await read_text_attachment(attachment)
                        if file_content:
                            if input_text:
                                input_text += f"\n\n【ファイル: {attachment.filename}】\n{file_content}"
                            else:
                                input_text = f"【ファイル: {attachment.filename}】\n{file_content}"
                            logger.info(f"添付ファイルの内容を追加: {attachment.filename}")
                
                if input_text:
                    # URL検出・警告
                    await check_content_for_urls(input_text, user, channel)
                    
                    # モデルを選択
                    model = PREMIUM_USER_MODEL if is_premium else FREE_USER_MODEL
                    
                    # 処理開始メッセージを送信
                    message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
                    await channel.send(f"{user.mention} 🤔 投稿内容について詳しく解説するね〜！ちょっと待っててね\n📎 元メッセージ: {message_link}")
                    
                    # 解説用プロンプトを読み込み
                    explain_prompt = None
                    prompt_path = script_dir / "prompt" / "question_explain.txt"
                    if prompt_path.exists():
                        with open(prompt_path, 'r', encoding='utf-8') as f:
                            explain_prompt = f.read()
                        logger.info("解説プロンプトファイルを使用")
                    else:
                        explain_prompt = "あなたはDiscordメッセージの内容について詳しく解説するアシスタントです。投稿内容をわかりやすく、丁寧に解説してください。専門用語があれば説明し、背景情報も補足してください。"
                        logger.info("フォールバック解説プロンプトを使用")
                    
                    # OpenAI APIで解説を生成
                    if client_openai:
                        try:
                            response = client_openai.chat.completions.create(
                                model=model,
                                messages=[
                                    {"role": "system", "content": explain_prompt},
                                    {"role": "user", "content": input_text}
                                ],
                                max_tokens=2000,
                                temperature=0.7
                            )
                            
                            explanation = response.choices[0].message.content
                            
                            # Discord文字数制限対応（2000文字以内に調整）
                            if len(explanation) > 1900:
                                explanation = explanation[:1900] + "..."
                            
                            # 結果を送信
                            embed = discord.Embed(
                                title="🤔 AI解説",
                                description=explanation,
                                color=0xFF6B35
                            )
                            
                            # 元の投稿内容も表示（短縮版）
                            original_content = message.content[:200] + "..." if len(message.content) > 200 else message.content
                            embed.add_field(
                                name="📝 元の投稿",
                                value=original_content,
                                inline=False
                            )
                            
                            await channel.send("💡 解説が完了したよ〜！")
                            await channel.send(embed=embed)
                            
                        except Exception as e:
                            logger.error(f"OpenAI API エラー (解説機能): {e}")
                            await channel.send(f"{user.mention} ❌ 解説の生成中にエラーが発生しました。")
                    else:
                        logger.error("エラー: OpenAI APIキーが設定されていません")
                        await channel.send(f"{user.mention} ❌ エラーが発生しました。管理者にお問い合わせください。")
                else:
                    await channel.send(f"{user.mention} ⚠️ メッセージに内容がありません。")
            
            # ✏️ 鉛筆：Obsidianメモ作成
            elif payload.emoji.name == '✏️':
                # メッセージ内容または添付ファイル、Embedからテキストを取得
                input_text = message.content
                
                # Embedがある場合は内容を抽出
                embed_content = extract_embed_content(message)
                if embed_content:
                    if input_text:
                        input_text += f"\n\n【Embed内容】\n{embed_content}"
                    else:
                        input_text = embed_content
                    logger.info("Embed内容を追加")
                
                # 添付ファイルがある場合、テキストファイルの内容を読み取り
                if message.attachments:
                    for attachment in message.attachments:
                        file_content = await read_text_attachment(attachment)
                        if file_content:
                            if input_text:
                                input_text += f"\n\n【ファイル: {attachment.filename}】\n{file_content}"
                            else:
                                input_text = f"【ファイル: {attachment.filename}】\n{file_content}"
                            logger.info(f"添付ファイルの内容を追加: {attachment.filename}")
                
                if input_text:
                    # URL検出・警告
                    await check_content_for_urls(input_text, user, channel)
                    
                    # 処理開始メッセージ
                    message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
                    await channel.send(f"{user.mention} 📝 メモを作るよ〜！ちょっと待っててね\n📎 元メッセージ: {message_link}")
                    
                    # モデルを選択
                    model = PREMIUM_USER_MODEL if is_premium else FREE_USER_MODEL
                    
                    # メモ用プロンプトを読み込み
                    memo_prompt = None
                    
                    # 1. ユーザーのカスタムプロンプトをチェック
                    if user_data and user_data.get('custom_prompt_memo'):
                        memo_prompt = user_data['custom_prompt_memo']
                        logger.info(f"ユーザー {user.name} のメモ用カスタムプロンプトを使用")
                    
                    # 2. カスタムプロンプトがない場合はデフォルトプロンプトファイルを使用
                    if not memo_prompt:
                        prompt_path = script_dir / "prompt" / "pencil_memo.txt"
                        if prompt_path.exists():
                            with open(prompt_path, 'r', encoding='utf-8') as f:
                                memo_prompt = f.read()
                            logger.info("デフォルトメモプロンプトファイルを使用")
                        else:
                            memo_prompt = "あなたはDiscordメッセージの内容をObsidianメモとして整理するアシスタントです。内容に忠実にメモ化してください。追加情報は加えず、原文を尊重してください。"
                            logger.info("フォールバックメモプロンプトを使用")
                    
                    # プロンプトにJSON出力指示を追加（カスタムプロンプトでも対応）
                    json_instruction = '\n\n出力はJSON形式で、以下のフォーマットに従ってください：\n{"english_title": "english_title_for_filename", "content": "メモの内容"}'
                    memo_prompt += json_instruction
                    
                    # OpenAI APIでメモを生成（JSONモード）
                    if client_openai:
                        try:
                            response = client_openai.chat.completions.create(
                                model=model,
                                messages=[
                                    {"role": "system", "content": memo_prompt},
                                    {"role": "user", "content": input_text}
                                ],
                                max_tokens=2000,
                                temperature=0.3,
                                response_format={"type": "json_object"}
                            )
                            
                            # JSONレスポンスをパース
                            response_content = response.choices[0].message.content
                            try:
                                memo_json = json.loads(response_content)
                                english_title = memo_json.get("english_title", "untitled_memo")
                                content = memo_json.get("content", input_text)
                            except json.JSONDecodeError:
                                logger.warning(f"JSON解析エラー、フォールバックを使用: {response_content}")
                                english_title = "untitled_memo"
                                content = input_text
                            
                            # ファイル名を生成（YYYYMMDD_HHMMSS_english_title.md）
                            now = datetime.now()
                            timestamp = now.strftime("%Y%m%d_%H%M%S")
                            # 英語タイトルを安全なファイル名に変換
                            safe_english_title = re.sub(r'[^A-Za-z0-9\-_]', '', english_title)
                            if not safe_english_title:
                                safe_english_title = "memo"
                            filename = f"{timestamp}_{safe_english_title}.md"
                            
                            # attachmentsフォルダにファイルを保存
                            attachments_dir = script_dir / "attachments"
                            attachments_dir.mkdir(exist_ok=True)
                            file_path = attachments_dir / filename
                            
                            # ファイル内容：コンテンツをそのまま保存
                            file_content = content
                            
                            # UTF-8でファイル保存
                            with open(file_path, 'w', encoding='utf-8') as f:
                                f.write(file_content)
                            
                            logger.info(f"メモファイル作成: {file_path}")
                            
                            try:
                                # 結果を送信
                                embed = discord.Embed(
                                    title="📝 Obsidianメモを作成しました",
                                    description=f"**ファイル名**: `{filename}`",
                                    color=0x7C3AED
                                )
                                
                                # 内容のプレビュー（最初の200文字）
                                preview = content[:200] + "..." if len(content) > 200 else content
                                embed.add_field(
                                    name="📄 内容プレビュー",
                                    value=preview,
                                    inline=False
                                )
                                
                                await channel.send(embed=embed)
                                
                                # ファイルをアップロード
                                with open(file_path, 'rb') as f:
                                    file_data = f.read()
                                
                                file_obj = io.BytesIO(file_data)
                                file_message = await channel.send("📝 メモファイルを作成しました！", file=discord.File(file_obj, filename=filename))
                                
                                # メモファイルに自動でリアクションを追加
                                reactions = ['👍', '❓', '❤️', '✏️', '📝']
                                for reaction in reactions:
                                    try:
                                        await file_message.add_reaction(reaction)
                                        await asyncio.sleep(0.5)  # Discord API レート制限対策
                                    except Exception as e:
                                        logger.warning(f"リアクション追加エラー ({reaction}): {e}")
                                
                                logger.info("メモファイルにリアクションを追加しました")
                                
                                # Discord投稿後、attachmentsフォルダの中身を削除
                                for attachment_file in attachments_dir.iterdir():
                                    if attachment_file.is_file():
                                        attachment_file.unlink()
                                        logger.info(f"添付ファイル削除: {attachment_file}")
                                
                            except Exception as upload_error:
                                logger.error(f"ファイル投稿エラー: {upload_error}")
                                # エラーが発生してもファイルは削除する
                                try:
                                    file_path.unlink()
                                    logger.info(f"エラー後のファイル削除: {file_path}")
                                except Exception as cleanup_error:
                                    logger.warning(f"ファイル削除エラー: {cleanup_error}")
                                raise upload_error
                            
                        except Exception as e:
                            logger.error(f"OpenAI API エラー (メモ機能): {e}")
                            await channel.send(f"{user.mention} ❌ メモの生成中にエラーが発生しました。")
                    else:
                        logger.error("エラー: OpenAI APIキーが設定されていません")
                        await channel.send(f"{user.mention} ❌ エラーが発生しました。管理者にお問い合わせください。")
                else:
                    await channel.send(f"{user.mention} ⚠️ メッセージに内容がありません。")
            
            # 📝 メモ：記事作成
            elif payload.emoji.name == '📝':
                # メッセージ内容または添付ファイル、Embedからテキストを取得
                input_text = message.content
                
                # Embedがある場合は内容を抽出
                embed_content = extract_embed_content(message)
                if embed_content:
                    if input_text:
                        input_text += f"\n\n【Embed内容】\n{embed_content}"
                    else:
                        input_text = embed_content
                    logger.info("Embed内容を追加")
                
                # 添付ファイルがある場合、テキストファイルの内容を読み取り
                if message.attachments:
                    for attachment in message.attachments:
                        file_content = await read_text_attachment(attachment)
                        if file_content:
                            if input_text:
                                input_text += f"\n\n【ファイル: {attachment.filename}】\n{file_content}"
                            else:
                                input_text = f"【ファイル: {attachment.filename}】\n{file_content}"
                            logger.info(f"添付ファイルの内容を追加: {attachment.filename}")
                
                if input_text:
                    # URL検出・警告
                    await check_content_for_urls(input_text, user, channel)
                    
                    # 処理開始メッセージ
                    message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
                    await channel.send(f"{user.mention} 📝 記事を作成するよ〜！ちょっと待っててね\n📎 元メッセージ: {message_link}")
                    
                    # モデルを選択
                    model = PREMIUM_USER_MODEL if is_premium else FREE_USER_MODEL
                    
                    # 記事用プロンプトを読み込み
                    article_prompt = None
                    
                    # 1. ユーザーのカスタムプロンプトをチェック
                    if user_data and user_data.get('custom_prompt_article'):
                        article_prompt = user_data['custom_prompt_article']
                        logger.info(f"ユーザー {user.name} のカスタムプロンプトを使用")
                    
                    # 2. カスタムプロンプトがない場合はデフォルトプロンプトファイルを使用
                    if not article_prompt:
                        prompt_path = script_dir / "prompt" / "article.txt"
                        if prompt_path.exists():
                            with open(prompt_path, 'r', encoding='utf-8') as f:
                                article_prompt = f.read()
                            logger.info("デフォルトプロンプトファイルを使用")
                        else:
                            article_prompt = "あなたは優秀なライターです。与えられた内容を元に、構造化された記事を作成してください。"
                            logger.info("フォールバックプロンプトを使用")
                    
                    # プロンプトにJSON出力指示を追加（既に含まれていない場合）
                    if '{"content":' not in article_prompt:
                        article_prompt += '\n\n出力はJSON形式で、以下のフォーマットに従ってください：\n{"content": "マークダウン形式の記事全文"}'
                    
                    # OpenAI APIで記事を生成（JSONモード）
                    if client_openai:
                        try:
                            response = client_openai.chat.completions.create(
                                model=model,
                                messages=[
                                    {"role": "system", "content": article_prompt},
                                    {"role": "user", "content": input_text}
                                ],
                                max_tokens=3000,
                                temperature=0.7,
                                response_format={"type": "json_object"}
                            )
                            
                            # JSONレスポンスをパース
                            response_content = response.choices[0].message.content
                            try:
                                article_json = json.loads(response_content)
                                content = article_json.get("content", response_content)
                            except json.JSONDecodeError:
                                logger.warning(f"JSON解析エラー、フォールバックを使用: {response_content}")
                                content = response_content
                            
                            # ファイル名を生成（YYYYMMDD_HHMMSS_article.md）
                            now = datetime.now()
                            timestamp = now.strftime("%Y%m%d_%H%M%S")
                            filename = f"{timestamp}_article.md"
                            
                            # attachmentsフォルダにファイルを保存
                            attachments_dir = script_dir / "attachments"
                            attachments_dir.mkdir(exist_ok=True)
                            file_path = attachments_dir / filename
                            
                            # UTF-8でファイル保存
                            with open(file_path, 'w', encoding='utf-8') as f:
                                f.write(content)
                            
                            logger.info(f"記事ファイル作成: {file_path}")
                            
                            try:
                                # 記事のタイトルを抽出（最初の#行）
                                lines = content.split('\n')
                                title = "記事"
                                for line in lines:
                                    if line.strip().startswith('# '):
                                        title = line.strip()[2:].strip()
                                        break
                                
                                # 結果を送信
                                embed = discord.Embed(
                                    title="📝 記事を作成しました",
                                    description=f"**タイトル**: {title}\n**ファイル名**: `{filename}`",
                                    color=0x00bfa5
                                )
                                
                                # 内容のプレビュー（最初の300文字）
                                preview = content[:300] + "..." if len(content) > 300 else content
                                embed.add_field(
                                    name="📄 内容プレビュー",
                                    value=f"```markdown\n{preview}\n```",
                                    inline=False
                                )
                                
                                await channel.send(embed=embed)
                                
                                # ファイルをアップロード
                                with open(file_path, 'rb') as f:
                                    file_data = f.read()
                                
                                file_obj = io.BytesIO(file_data)
                                file_message = await channel.send("📝 記事ファイルです！", file=discord.File(file_obj, filename=filename))
                                
                                # 記事ファイルに自動でリアクションを追加
                                reactions = ['👍', '❓', '❤️', '✏️', '📝']
                                for reaction in reactions:
                                    try:
                                        await file_message.add_reaction(reaction)
                                        await asyncio.sleep(0.5)  # Discord API レート制限対策
                                    except Exception as e:
                                        logger.warning(f"リアクション追加エラー ({reaction}): {e}")
                                
                                logger.info("記事ファイルにリアクションを追加しました")
                                
                                # Discord投稿後、attachmentsフォルダの中身を削除
                                for attachment_file in attachments_dir.iterdir():
                                    if attachment_file.is_file():
                                        attachment_file.unlink()
                                        logger.info(f"添付ファイル削除: {attachment_file}")
                                
                            except Exception as upload_error:
                                logger.error(f"ファイル投稿エラー: {upload_error}")
                                # エラーが発生してもファイルは削除する
                                try:
                                    file_path.unlink()
                                    logger.info(f"エラー後のファイル削除: {file_path}")
                                except Exception as cleanup_error:
                                    logger.warning(f"ファイル削除エラー: {cleanup_error}")
                                raise upload_error
                            
                        except Exception as e:
                            logger.error(f"OpenAI API エラー (記事機能): {e}")
                            await channel.send(f"{user.mention} ❌ 記事の生成中にエラーが発生しました。")
                    else:
                        logger.error("エラー: OpenAI APIキーが設定されていません")
                        await channel.send(f"{user.mention} ❌ エラーが発生しました。管理者にお問い合わせください。")
                else:
                    await channel.send(f"{user.mention} ⚠️ メッセージに内容がありません。")

@bot.event
async def on_message(message):
    """メッセージ受信時の処理 - 自動リアクション追加"""
    # Botのメッセージは無視
    if message.author.bot:
        return
    
    # チャンネルが有効かチェック
    server_id = str(message.guild.id) if message.guild else None
    channel_id = str(message.channel.id)
    
    if server_id and is_channel_active(server_id, channel_id):
        try:
            # 音声・動画ファイルがあるかチェック
            has_audio = False
            has_non_audio = False
            
            if message.attachments:
                AUDIO_EXTS = ('.mp3', '.m4a', '.ogg', '.webm', '.wav')
                VIDEO_EXTS = ('.mp4',)
                for attachment in message.attachments:
                    filename_lower = attachment.filename.lower()
                    if filename_lower.endswith(AUDIO_EXTS) or filename_lower.endswith(VIDEO_EXTS):
                        has_audio = True
                    else:
                        has_non_audio = True
            
            # メッセージ内容があるかチェック
            has_content = bool(message.content.strip())
            
            # 最初のリアクション前に1秒待機（エラー回避のため）
            await asyncio.sleep(1.0)
            
            # 音声ファイルのみの場合はマイクだけ
            if has_audio and not has_non_audio and not has_content:
                await message.add_reaction('🎤')
                await asyncio.sleep(0.3)
            else:
                # その他の場合は基本リアクション
                basic_reactions = ['👍', '❓', '❤️', '✏️', '📝']
                
                # リアクションを追加
                for emoji in basic_reactions:
                    await message.add_reaction(emoji)
                    await asyncio.sleep(0.3)  # リアクション追加の間隔
                
                # 音声ファイルがある場合はマイクも追加
                if has_audio:
                    await message.add_reaction('🎤')
                    await asyncio.sleep(0.3)
            
            logger.info(f"自動リアクション追加完了: {message.channel.name} - {message.author.name}")
            
        except Exception as e:
            logger.error(f"自動リアクション追加エラー: {e}")
    
    # コマンドの処理を継続
    await bot.process_commands(message)



if __name__ == "__main__":
    if TOKEN is None:
        logger.error("エラー: DISCORD_BOT_TOKEN 環境変数が設定されていません")
    else:
        try:
            logger.info("Botを起動しています...")
            bot.run(TOKEN)
        except Exception as e:
            logger.error(f"Bot起動エラー: {e}")
            import traceback
            logger.error(traceback.format_exc())