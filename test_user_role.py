#!/usr/bin/env python3
"""
特定ユーザーのロール検出テスト用スクリプト
"""
import discord
from discord.ext import commands
import json
import os
from pathlib import Path
from dotenv import load_dotenv

# 環境変数設定
script_dir = Path(__file__).parent
env_path = script_dir / '.env'
load_dotenv(env_path, override=True)

TOKEN = os.getenv('DISCORD_BOT_TOKEN')

# 設定読み込み
with open(script_dir / "settings.json", 'r', encoding='utf-8') as f:
    settings = json.load(f)

# Intentsの設定
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

# Botの初期化
bot = commands.Bot(command_prefix='!', intents=intents)

async def test_user_role(user_id):
    """特定ユーザーのロールを検出"""
    try:
        # コミュニティサーバーを取得
        community_guild = bot.get_guild(int(settings.get("community_server_id")))
        if not community_guild:
            print(f"❌ Community server not found: {settings.get('community_server_id')}")
            return
        
        print(f"📊 サーバー: {community_guild.name}")
        print(f"👥 メンバー数: {community_guild.member_count}")
        print(f"🔍 検索対象ユーザーID: {user_id}")
        print("-" * 50)
        
        # まずget_member()で試す
        member = community_guild.get_member(int(user_id))
        if member:
            print(f"✅ get_member()で発見: {member.name}#{member.discriminator}")
        else:
            print(f"⚠️ get_member()では見つからない")
            print(f"🔄 fetch_member()で再試行中...")
            
            try:
                member = await community_guild.fetch_member(int(user_id))
                print(f"✅ fetch_member()で発見: {member.name}#{member.discriminator}")
            except discord.NotFound:
                print(f"❌ ユーザーがサーバーに存在しません")
                return
            except discord.Forbidden:
                print(f"❌ 権限不足でユーザー情報を取得できません")
                return
            except Exception as e:
                print(f"❌ エラー: {e}")
                return
        
        # ロール情報を表示
        print(f"👤 ユーザー: {member.name}#{member.discriminator}")
        print(f"🆔 ユーザーID: {member.id}")
        print(f"📅 サーバー参加日: {member.joined_at}")
        print(f"🏆 最高ロール: {member.top_role.name}")
        print(f"📜 所持ロール:")
        
        for role in member.roles:
            print(f"  - {role.name} (ID: {role.id})")
        
        # プレミアムロールチェック
        premium_role_id = int(settings.get("premium_role_id"))
        print(f"\n🎯 プレミアムロールID: {premium_role_id}")
        
        has_premium_role = any(role.id == premium_role_id for role in member.roles)
        print(f"💎 プレミアムロール所持: {'✅ YES' if has_premium_role else '❌ NO'}")
        
        # オーナーチェック
        if int(user_id) == community_guild.owner_id:
            print(f"👑 サーバーオーナー: ✅ YES")
        else:
            print(f"👑 サーバーオーナー: ❌ NO")
            
        # 設定ファイルオーナーチェック
        owner_user_id = settings.get("owner_user_id")
        if owner_user_id and str(user_id) == str(owner_user_id):
            print(f"⚙️ 設定ファイルオーナー: ✅ YES")
        else:
            print(f"⚙️ 設定ファイルオーナー: ❌ NO")
            
    except Exception as e:
        print(f"❌ エラー: {e}")
        import traceback
        traceback.print_exc()

@bot.event
async def on_ready():
    print(f'🤖 {bot.user} としてログインしました')
    print(f'📡 サーバー数: {len(bot.guilds)}')
    
    # テスト実行
    await test_user_role(960439757345804308)
    
    # Bot終了
    await bot.close()

if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_BOT_TOKEN が設定されていません")
    else:
        bot.run(TOKEN)