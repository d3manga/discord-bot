import os
import re
import requests
import discord
import traceback
from discord.ext import commands, tasks
from datetime import datetime
from googleapiclient.discovery import build
from flask import Flask
from threading import Thread
from waitress import serve
from typing import cast

# ───────────────────────────────────────────────
# INTENTS
# ───────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

client = commands.Bot(command_prefix="++", intents=intents)

# ───────────────────────────────────────────────
# SECRETS (Replit Secrets)
# ───────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
BLOGGER_API_KEY = os.getenv("BLOGGER_API_KEY", "")
BLOG_ID = os.getenv("BLOG_ID", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
NOTIFICATION_ROLE_ID = os.getenv("NOTIFICATION_ROLE_ID", "")

# Blogger API client
blog = build("blogger", "v3", developerKey=BLOGGER_API_KEY)

# Discord’a gönderilen duyuru mesajlarını RAM’de tutacağız:
# {blog_post_id: discord_message_id}
sent_messages = {}


# ───────────────────────────────────────────────
# SERİ KANALI BULAN FONKSİYON
# ───────────────────────────────────────────────
def get_series_channel(guild: discord.Guild, series_name: str) -> discord.TextChannel | None:
    """
    Discord sunucusunda seri ismiyle eşleşen kanalı bulur.
    Case-insensitive exact matching yapar.
    
    Args:
        guild: Discord sunucusu
        series_name: Blogger'dan gelen seri etiketi
    
    Returns:
        Eşleşen text channel veya None
    
    Requirements: 1.1, 1.4, 2.1, 2.3
    """
    if not series_name:
        return None
    
    series_lower = series_name.lower()
    for channel in guild.text_channels:
        if channel.name.lower() == series_lower:
            return channel
    return None


# ───────────────────────────────────────────────
# SERİ ROLÜ BULAN FONKSİYON
# ───────────────────────────────────────────────
def get_series_role(guild: discord.Guild, series_name: str) -> discord.Role | None:
    """
    Discord sunucusunda seri ismiyle eşleşen rolü bulur.
    Case-insensitive exact matching yapar.
    
    Args:
        guild: Discord sunucusu
        series_name: Blogger'dan gelen seri etiketi
    
    Returns:
        Eşleşen rol veya None (eşleşme yoksa)
    
    Requirements: 1.1, 1.4, 2.1, 2.3
    """
    if not series_name:
        return None
    
    series_lower = series_name.lower()
    for role in guild.roles:
        if role.name.lower() == series_lower:
            return role
    return None


# ───────────────────────────────────────────────
# SERİDEN KAPAK GÖRSELİ ALAN FONKSİYON
# ───────────────────────────────────────────────
def get_series_cover(manga_name: str):
    """
    Belirtilen manga etiketinin SERİ sayfasındaki ilk gönderisinden
    ilk <img src="..."> görselini kapak olarak döndürür.
    Restart sonrası kayıt tutulmaz (sadece runtime için).
    """
    try:
        url = (f"https://www.googleapis.com/blogger/v3/blogs/"
               f"{BLOG_ID}/posts?labels={manga_name}&key={BLOGGER_API_KEY}")
        data = requests.get(url).json()

        if "items" not in data or not data["items"]:
            return None

        first_post = data["items"][0]
        content = first_post.get("content", "")

        img_match = re.search(r'<img[^>]+src="([^"]+)"', content)
        if img_match:
            return img_match.group(1)

        return None

    except Exception as e:
        print("Series cover error:", e)
        return None


def extract_first_image_src(html_content: str) -> str | None:
    """
    HTML içeriğinden ilk img etiketinin src attribute'unu çıkarır.
    
    Args:
        html_content: HTML içeriği
    
    Returns:
        İlk img src URL'si veya None (img yoksa)
    
    Requirements: 3.2, 3.4
    """
    if not html_content:
        return None
    
    img_match = re.search(r'<img[^>]+src="([^"]+)"', html_content)
    if img_match:
        return img_match.group(1)
    return None


def get_series_cover_by_label(series_name: str) -> str | None:
    """
    Blogger'da hem seri ismi hem "series" etiketine sahip posttan
    kapak fotoğrafını çeker.
    
    Args:
        series_name: Manga/webtoon seri ismi
    
    Returns:
        Kapak fotoğrafı URL'si veya None
    
    Requirements: 3.1, 3.2, 3.4
    """
    if not series_name:
        return None
    
    try:
        # Blogger API'den hem seri ismi hem "series" etiketli post ara
        # labels parametresi virgülle ayrılmış etiketleri AND olarak arar
        labels = f"{series_name},series"
        url = (f"https://www.googleapis.com/blogger/v3/blogs/"
               f"{BLOG_ID}/posts?labels={labels}&key={BLOGGER_API_KEY}")
        data = requests.get(url).json()

        if "items" not in data or not data["items"]:
            return None

        first_post = data["items"][0]
        content = first_post.get("content", "")

        # İlk img src'yi döndür
        return extract_first_image_src(content)

    except Exception as e:
        print(f"[get_series_cover_by_label] Hata: {e}")
        return None


# ───────────────────────────────────────────────
# BOT AÇILDIĞINDA
# ───────────────────────────────────────────────
@client.event
async def on_ready():
    print("Bot aktif!")
    fetchUpdates.start()
    await client.change_presence(activity=discord.Game(name="Manga Okuyor..."))


# ───────────────────────────────────────────────
# ++search KOMUTU
# ───────────────────────────────────────────────
@client.command()
async def search(ctx, *, arg):
    try:
        q = arg.replace(" ", "%")
        url = (f"https://www.googleapis.com/blogger/v3/blogs/"
               f"{BLOG_ID}/posts/search?q={q}&key={BLOGGER_API_KEY}")
        data = requests.get(url).json()

        now = datetime.now().strftime("%H:%M:%S")
        embed = discord.Embed(
            title="🔍 Arama Sonuçları",
            description=f"Kontrol zamanı: {now}\n",
            color=0x1abc9c,
        )

        items = data.get("items", [])
        description = f"Kontrol zamanı: {now}\n"
        if items:
            for i, item in enumerate(items):
                description += f"{i + 1}. [{item['title']}]({item['url']})\n"
        else:
            description += "Sonuç bulunamadı."

        embed.description = description

        await ctx.send(embed=embed)

    except Exception as e:
        print("Hata:", e)
        await ctx.send("Arama hatası oluştu.")


# ───────────────────────────────────────────────
# FETCH UPDATES – YENİ BÖLÜM TAKİBİ
# ───────────────────────────────────────────────
# Global değişken olarak son post zamanını tutuyoruz
client.lastPostTime = None  # type: ignore


@tasks.loop(seconds=10.0)
async def fetchUpdates():
    global sent_messages

    # 1) Blogger API çağrısı ayrı try içinde (çökmesin)
    try:
        posts = blog.posts().list(blogId=BLOG_ID, maxResults=5).execute()
    except Exception as e:
        print("[fetchUpdates] Google API hatası:", e)
        traceback.print_exc()
        # Loop ölmesin, sadece bu turu atla
        return

    try:
        # 2) Gelen veriyi kontrol et
        if "items" not in posts or not posts["items"]:
            return

        raw_channel = client.get_channel(CHANNEL_ID)
        if raw_channel is None:
            print(
                "[fetchUpdates] Discord channel bulunamadı. CHANNEL_ID doğru mu?"
            )
            return

        if not hasattr(raw_channel, "send"):
            print("[fetchUpdates] Channel 'send' metodunu desteklemiyor.")
            return

        channel = cast(discord.TextChannel, raw_channel)

        latest = posts["items"][0]
        published = latest["published"]
        title = latest["title"]
        url = latest["url"]
        labels = latest.get("labels", [])
        post_id = latest["id"]

        # İlk başlatma → sadece zamanı kaydet
        if client.lastPostTime is None:  # type: ignore
            client.lastPostTime = published  # type: ignore
            return  # İlk çalışmada duyuru atma

        # Yeni bölüm kontrolü
        if client.lastPostTime != published:  # type: ignore
            # ─────────────────────────────
            # 1) Manga adını label’den al
            # ─────────────────────────────
            manga_title = None
            for label in labels:
                low = label.lower()
                if low == "chapter":
                    continue
                if low.replace(" ", "").isdigit():
                    continue
                if low.startswith("chapter"):
                    continue
                manga_title = label

            if manga_title is None:
                manga_title = "Bilinmeyen Seri"

            # ─────────────────────────────
            # 2) Bölüm numarası
            # ─────────────────────────────
            m = re.search(r"(\d+)", title)
            if m:
                chapter_number = m.group(1)
            else:
                chapter_number = title

            # ─────────────────────────────
            # 3) Kapak görseli (Requirements: 3.3, 4.2, 4.3)
            # ─────────────────────────────
            # Yeni fonksiyon: "series" etiketli posttan kapak al
            # Error handling: Requirements 4.2, 4.3
            DEFAULT_COVER_IMAGE = (
                "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEjuqd3-dB8OcbTh9M-R8LGZsRmnkvHxn4v1fv4nUAJqWjEBXyafKIV2ZNdFoCC6JtMciO_W14XKSCmHQIzsz9WG1PVtc9WC5sssKpUyyWSSnYFEuPorsfSlOCltsVdwMoVevKL3ARV73LFlSwZpMdvtV8Ytep4miek6BSrBWZPHD58w_uAM7qIJy4LzVE_5/s1600/321b9acf8c8-f6c1-4ede-882a-83125281a421---Kopya.png"
            )
            try:
                cover_image = get_series_cover_by_label(manga_title)
            except Exception as e:
                print(f"[fetchUpdates] Kapak fotoğrafı hatası: {e}")
                cover_image = None
            
            if not cover_image:
                # Fallback: varsayılan kapak görseli
                cover_image = DEFAULT_COVER_IMAGE

            # ─────────────────────────────
            # 4) Embed oluştur
            # ─────────────────────────────
            embed = discord.Embed(
                title="📢 Yeni Bölüm Yayınlandı!",
                description=
                f"**{manga_title}** için yeni bir bölüm yayınlandı!",
                color=0x00C2FF,
            )

            embed.set_thumbnail(url=(
                "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEhnSnG9Zm2z6BVlu5deBMX7YXV3e736-rkyuE2wucmhhTVFWt-94GlcEMQ40yBuYH2tozmNbFqgpohfDdGS6VIs-36SbvmHS9x4p-HMrn-pR1ikw8dQB9JYDQkukrKaWoZ5impyTfQggUWltIUmhe7OrT9dMSlkEuYAlnHfotuvyeoxIsLBVETdooVagSX_/s1600/gip3123hy.gif"
            ))

            embed.add_field(
                name="📘 Seri İsmi",
                value=f"**{manga_title}**",
                inline=False,
            )

            embed.add_field(
                name="📄 Bölüm",
                value=f"**Bölüm {chapter_number}**",
                inline=False,
            )

            embed.add_field(
                name="🔗 Bölüm Linki",
                value=f"[Bölümü Aç]({url})",
                inline=False,
            )

            embed.set_image(url=cover_image)

            embed.set_footer(
                text=f"D3 Manga • Yayınlanma: {published}",
                icon_url=
                ("https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEjb6KH5VdssQRFuN8X1CPZs1y7B2gCnBQfb0YMx4PqsqPioba6vm2SK2-wNvx-1Vc2N5Lkdr7iCo03CXnP6UWsTLwxr8IBY3hl-102Q_vZNIXdYVj7aeTUGqv8it8XmPmDN3wIb1Z6bTEWwOyFDB7zLkLoMW7gk5feZfAcQzSPnIl-AYkvPY6y0xAsM3JnY/s1600/dragon%20%282%29.png"
                 ),
            )

            # ─────────────────────────────
            # 5) Seri kanalını bul ve mesaj gönder
            # ─────────────────────────────
            # Requirements: 1.2, 1.3, 4.1, 4.3
            # Error handling: Requirements 4.1, 4.3
            try:
                series_channel = get_series_channel(channel.guild, manga_title)
            except Exception as e:
                print(f"[fetchUpdates] Kanal arama hatası: {e}")
                series_channel = None
            
            if series_channel:
                # Seri kanalı bulundu - @everyone ile gönder
                target_channel = series_channel
                mention = "@everyone"
            else:
                # Seri kanalı bulunamadı - fallback kanala @Tüm Seriler rolü ile gönder
                target_channel = channel  # Fallback: varsayılan CHANNEL_ID
                # Tüm Seriler rolünü bul
                try:
                    tum_seriler_role = discord.utils.get(channel.guild.roles, name="Tüm Seriler")
                    if tum_seriler_role:
                        mention = f"<@&{tum_seriler_role.id}>"
                    else:
                        mention = None
                except Exception as e:
                    print(f"[fetchUpdates] Rol arama hatası: {e}")
                    mention = None
            
            # Mesajı gönder
            try:
                if mention:
                    msg = await target_channel.send(content=mention, embed=embed)
                else:
                    msg = await target_channel.send(embed=embed)
            except Exception as e:
                # Fallback: mention başarısız olursa sadece embed gönder
                print(f"[fetchUpdates] Mesaj gönderme hatası: {e}")
                msg = await target_channel.send(embed=embed)
            sent_messages[post_id] = msg.id

            client.lastPostTime = published  # type: ignore

        # ─────────────────────────────
        # 5) Silinen postları kontrol et
        # ─────────────────────────────
        current_ids = [item["id"] for item in posts["items"]]

        for saved_post_id, saved_message_id in list(sent_messages.items()):
            if saved_post_id not in current_ids:
                try:
                    old_msg = await channel.fetch_message(saved_message_id)
                    await old_msg.delete()
                    print(f"Discord mesajı silindi: {saved_post_id}")
                except Exception as e:
                    print(
                        f"Mesaj silinemedi (zaten silinmiş olabilir): {saved_post_id} -> {e}"
                    )
                del sent_messages[saved_post_id]

    except Exception as e:
        # Bu en dış katman: NE OLURSA OLSUN loop ölmesin
        print("[fetchUpdates] Beklenmeyen genel hata:", e)
        traceback.print_exc()


# ───────────────────────────────────────────────
# KEEP ALIVE (Replit 7/24 için Flask server)
# ───────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot aktif!"


def run_discord_bot():
    """Discord botunu ayrı bir thread'de çalıştır"""
    print("Starting Discord bot...")
    client.run(DISCORD_TOKEN)


# ───────────────────────────────────────────────
# BOTU ÇALIŞTIR
# ───────────────────────────────────────────────
if __name__ == "__main__":
    # Discord botunu background thread'de başlat
    bot_thread = Thread(target=run_discord_bot, daemon=True)
    bot_thread.start()

    # Waitress server'ı main thread'de başlat (port 'in ilk açılması için)
    print("Starting Waitress server on 0.0.0.0:5000")
    print("Bot is ready for 24/7 operation!")
    serve(app, host="0.0.0.0", port=5000, threads=4)
