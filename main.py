import os
import re
import random
import requests
import discord
import traceback
from discord.ext import commands, tasks
from datetime import datetime, timezone
from googleapiclient.discovery import build
from flask import Flask
from threading import Thread
from waitress import serve
from typing import cast

# ───────────────────────────────────────────────
# EMBED RENKLERİ (Rastgele seçilecek)
# ───────────────────────────────────────────────
EMBED_COLORS = [
    0xFF6B6B,  # Kırmızı
    0x4ECDC4,  # Turkuaz
    0x45B7D1,  # Mavi
    0x96CEB4,  # Yeşil
    0xFECE00,  # Sarı
    0xDDA0DD,  # Mor
    0xFF8C42,  # Turuncu
    0x98D8C8,  # Mint
    0xF7DC6F,  # Altın
    0xBB8FCE,  # Lavanta
    0x85C1E9,  # Açık Mavi
    0xF1948A,  # Pembe
]

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
async def get_series_channel(guild: discord.Guild, series_name: str):
    """
    Discord sunucusunda seri ismiyle eşleşen kanalı veya thread'i bulur.
    Case-insensitive exact matching yapar.
    Önce text channel'lara, sonra forum thread'lerine bakar.
    
    Args:
        guild: Discord sunucusu
        series_name: Blogger'dan gelen seri etiketi
    
    Returns:
        Eşleşen channel/thread veya None
    
    Requirements: 1.1, 1.4, 2.1, 2.3
    """
    if not series_name:
        return None
    
    series_lower = series_name.lower()
    print(f"[get_series_channel] Aranan seri: '{series_name}' -> '{series_lower}'")
    
    # 1. Önce text channel'lara bak
    print(f"[get_series_channel] Mevcut kanallar: {[ch.name for ch in guild.text_channels]}")
    for channel in guild.text_channels:
        if channel.name.lower() == series_lower:
            print(f"[get_series_channel] TEXT CHANNEL BULUNDU: #{channel.name}")
            return channel
    
    # 2. Forum kanallarındaki thread'lere bak
    for channel in guild.channels:
        if isinstance(channel, discord.ForumChannel):
            print(f"[get_series_channel] Forum kanalı bulundu: {channel.name}")
            
            # Cache'deki aktif thread'leri kontrol et
            print(f"[get_series_channel] Cache'deki thread'ler: {[t.name for t in channel.threads]}")
            for thread in channel.threads:
                if thread.name.lower() == series_lower:
                    print(f"[get_series_channel] FORUM THREAD BULUNDU (cache): {thread.name}")
                    return thread
            
            # Aktif thread'leri API'den çek
            try:
                active_threads = await guild.active_threads()
                print(f"[get_series_channel] Aktif thread'ler: {[t.name for t in active_threads]}")
                for thread in active_threads:
                    print(f"[get_series_channel] Thread kontrol: '{thread.name.lower()}' == '{series_lower}' ?")
                    if thread.name.lower() == series_lower:
                        print(f"[get_series_channel] FORUM THREAD BULUNDU (aktif): {thread.name}")
                        return thread
            except Exception as e:
                print(f"[get_series_channel] Aktif thread hatası: {e}")
            
            # Arşivlenmiş thread'leri de kontrol et
            try:
                async for thread in channel.archived_threads(limit=100):
                    if thread.name.lower() == series_lower:
                        print(f"[get_series_channel] ARŞİVLENMİŞ THREAD BULUNDU: {thread.name}")
                        return thread
            except Exception as e:
                print(f"[get_series_channel] Arşiv thread hatası: {e}")
    
    print(f"[get_series_channel] Eşleşen kanal/thread bulunamadı")
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
        # Blogger API'den hem seri ismi hem "Series" etiketli post ara
        # NOT: Blogger API case-sensitive, "Series" büyük S ile
        labels = f"{series_name},Series"
        url = (f"https://www.googleapis.com/blogger/v3/blogs/"
               f"{BLOG_ID}/posts?labels={labels}&key={BLOGGER_API_KEY}")
        print(f"[get_series_cover_by_label] Aranan etiketler: {labels}")
        data = requests.get(url).json()
        print(f"[get_series_cover_by_label] API yanıtı: {data.get('items', 'YOK')[:1] if data.get('items') else 'BOŞ'}")

        if "items" not in data or not data["items"]:
            # Küçük harfle de dene
            labels_lower = f"{series_name},series"
            url_lower = (f"https://www.googleapis.com/blogger/v3/blogs/"
                        f"{BLOG_ID}/posts?labels={labels_lower}&key={BLOGGER_API_KEY}")
            print(f"[get_series_cover_by_label] Küçük harfle deneniyor: {labels_lower}")
            data = requests.get(url_lower).json()
        
        if "items" not in data or not data["items"]:
            print(f"[get_series_cover_by_label] '{series_name}' + Series/series etiketli post bulunamadı")
            return None

        first_post = data["items"][0]
        content = first_post.get("content", "")
        print(f"[get_series_cover_by_label] Post bulundu: {first_post.get('title', 'Başlık yok')}")

        # İlk img src'yi döndür
        img_url = extract_first_image_src(content)
        print(f"[get_series_cover_by_label] Bulunan resim: {img_url[:50] if img_url else 'YOK'}...")
        return img_url

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
# ++tumseriler KOMUTU - Tüm serileri listele (Sadece yöneticiler)
# ───────────────────────────────────────────────
@client.command()
@commands.has_permissions(administrator=True)
async def seriler(ctx):
    """Blogger'daki tüm serileri listeler (Series etiketli postlar) - Sadece yöneticiler"""
    try:
        loading_msg = await ctx.send("📚 Seriler yükleniyor...")
        
        # Blogger API'den "Series" etiketli tüm postları çek
        url = (f"https://www.googleapis.com/blogger/v3/blogs/"
               f"{BLOG_ID}/posts?labels=Series&maxResults=50&key={BLOGGER_API_KEY}")
        data = requests.get(url).json()
        
        items = data.get("items", [])
        
        if not items:
            await loading_msg.edit(content="❌ Hiç seri bulunamadı.")
            return
        
        # Her seri için ayrı embed gönder
        await loading_msg.delete()
        
        for item in items:
            title = item.get("title", "Bilinmeyen")
            post_url = item.get("url", "#")
            labels = item.get("labels", [])
            content = item.get("content", "")
            
            # Durum ve tür bilgisi
            status = "📖 Devam Ediyor"
            status_color = 0x3498db  # Mavi
            if "Devam ediyor" in labels:
                status = "🟢 Devam Ediyor"
                status_color = 0x2ecc71  # Yeşil
            elif "Tamamlandı" in labels:
                status = "✅ Tamamlandı"
                status_color = 0x9b59b6  # Mor
            elif "Bırakıldı" in labels:
                status = "❌ Bırakıldı"
                status_color = 0xe74c3c  # Kırmızı
            
            # Kapak resmini al
            cover_img = extract_first_image_src(content)
            
            # Türleri filtrele
            skip_labels = {"series", "devam ediyor", "tamamlandı", "bırakıldı", "chapter"}
            genres = [l for l in labels if l.lower() not in skip_labels and l != title]
            genre_text = " • ".join(genres[:5]) if genres else "Belirtilmemiş"
            
            # Embed oluştur - Compact tasarım (thumbnail ile)
            embed = discord.Embed(
                title=f"{title}",
                description=f"{status}\n🏷️ {genre_text}",
                color=status_color,
            )
            
            # Küçük kare thumbnail (sağ tarafta, sabit boyut)
            if cover_img:
                embed.set_thumbnail(url=cover_img)
            
            # Butonlar ekle
            view = discord.ui.View()
            
            # Seriye Git butonu
            view.add_item(discord.ui.Button(
                label="📚 Seriye Git",
                style=discord.ButtonStyle.link,
                url=post_url
            ))
            
            # Thread'i bul ve buton ekle
            series_thread = await get_series_channel(ctx.guild, title)
            if series_thread:
                # Discord thread URL'si
                thread_url = f"https://discord.com/channels/{ctx.guild.id}/{series_thread.id}"
                view.add_item(discord.ui.Button(
                    label="💬 Duyurular",
                    style=discord.ButtonStyle.link,
                    url=thread_url
                ))
            
            await ctx.send(embed=embed, view=view)
        
        # Özet mesajı
        await ctx.send(f"📊 Toplam **{len(items)}** seri listelendi!")
        
    except Exception as e:
        print(f"[tumseriler] Hata: {e}")
        await ctx.send("❌ Seriler yüklenirken hata oluştu.")


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
            # Atlanacak etiketler (genel kategoriler)
            skip_labels = {
                "chapter", "series", "devam ediyor", "tamamlandı", "bırakıldı",
                "dram", "korku", "gizem", "psikoloji", "shounen", "shoujo",
                "seinen", "josei", "aksiyon", "macera", "komedi", "romantik",
                "fantastik", "bilim kurgu", "spor", "müzik", "okul", "günlük yaşam"
            }
            for label in labels:
                low = label.lower()
                if low in skip_labels:
                    continue
                if low.replace(" ", "").isdigit():
                    continue
                if low.startswith("chapter"):
                    continue
                # İlk uygun etiketi al ve çık
                manga_title = label
                break

            if manga_title is None:
                manga_title = "Bilinmeyen Seri"
            
            print(f"[fetchUpdates] Manga title: {manga_title}, Labels: {labels}")

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
            # 4) Embed oluştur (Yeni tasarım)
            # ─────────────────────────────
            embed = discord.Embed(
                title=f"📖 {manga_title}",
                description=f"**Bölüm {chapter_number}** yayınlandı!\n\n"
                           f"━━━━━━━━━━━━━━━━━━━━━━",
                color=random.choice(EMBED_COLORS),  # Rastgele renk
            )

            # Seri ve bölüm bilgisi yan yana
            embed.add_field(
                name="📚 Seri",
                value=f"`{manga_title}`",
                inline=True,
            )

            embed.add_field(
                name="📄 Bölüm",
                value=f"`{chapter_number}`",
                inline=True,
            )

            # Boş alan (satır atlama için)
            embed.add_field(
                name="\u200b",
                value="\u200b",
                inline=True,
            )

            embed.set_image(url=cover_image)

            embed.set_footer(
                text="D3 Manga",
                icon_url="https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEjb6KH5VdssQRFuN8X1CPZs1y7B2gCnBQfb0YMx4PqsqPioba6vm2SK2-wNvx-1Vc2N5Lkdr7iCo03CXnP6UWsTLwxr8IBY3hl-102Q_vZNIXdYVj7aeTUGqv8it8XmPmDN3wIb1Z6bTEWwOyFDB7zLkLoMW7gk5feZfAcQzSPnIl-AYkvPY6y0xAsM3JnY/s1600/dragon%20%282%29.png"
            )
            
            # Timestamp ekle
            embed.timestamp = datetime.now(timezone.utc)

            # ─────────────────────────────
            # 4.1) Butonlar oluştur
            # ─────────────────────────────
            view = discord.ui.View()
            
            # Bölümü Oku butonu
            read_button = discord.ui.Button(
                label="📖 Bölümü Oku",
                style=discord.ButtonStyle.link,
                url=url
            )
            view.add_item(read_button)
            
            # Seri sayfası butonu (Blogger'da seri etiketine git)
            series_url = f"https://d3manga.blogspot.com/search/label/{manga_title.replace(' ', '%20')}"
            series_button = discord.ui.Button(
                label="📚 Tüm Bölümler",
                style=discord.ButtonStyle.link,
                url=series_url
            )
            view.add_item(series_button)

            # ─────────────────────────────
            # 5) Mesajları gönder (İki kanala aynı anda)
            # ─────────────────────────────
            # 1. Ana kanala (CHANNEL_ID) @Tüm Seriler rolü ile gönder
            # 2. Seri thread'i varsa oraya da @everyone ile gönder
            
            # Önce seri thread'ini bul (async fonksiyon)
            series_thread = None
            try:
                series_thread = await get_series_channel(channel.guild, manga_title)
                if series_thread:
                    print(f"[fetchUpdates] Seri thread bulundu: {series_thread.name}")
            except Exception as e:
                print(f"[fetchUpdates] Thread arama hatası: {e}")
            
            # 1. Ana kanala (CHANNEL_ID) gönder - @Tüm Seriler ile
            tum_seriler_mention = None
            try:
                tum_seriler_role = discord.utils.get(channel.guild.roles, name="Tüm Seriler")
                if tum_seriler_role:
                    tum_seriler_mention = f"<@&{tum_seriler_role.id}>"
            except Exception as e:
                print(f"[fetchUpdates] Rol arama hatası: {e}")
            
            try:
                if tum_seriler_mention:
                    msg = await channel.send(content=tum_seriler_mention, embed=embed, view=view)
                else:
                    msg = await channel.send(embed=embed, view=view)
                sent_messages[post_id] = msg.id
                print(f"[fetchUpdates] Ana kanala mesaj gönderildi: {manga_title}")
            except Exception as e:
                print(f"[fetchUpdates] Ana kanal mesaj hatası: {e}")
            
            # 2. Seri thread'i varsa oraya da gönder - @everyone ile
            if series_thread:
                try:
                    await series_thread.send(content="@everyone", embed=embed, view=view)
                    print(f"[fetchUpdates] Thread'e mesaj gönderildi: {series_thread.name}")
                except Exception as e:
                    print(f"[fetchUpdates] Thread mesaj hatası: {e}")

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
