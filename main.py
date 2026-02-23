import os
import re
import random
import requests
import discord
from discord.ext import commands, tasks
from datetime import datetime
from urllib.parse import unquote

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

client = commands.Bot(command_prefix="++", intents=intents)

# ───────────────────────────────────────────────
# SECRETS
# ───────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
SERIES_THREAD_CHANNEL_ID = int(os.getenv("SERIES_THREAD_CHANNEL_ID", "0"))
ZEBZETOON_CSV_URL = "https://zebzetoon.vercel.app/liste.csv"
ZEBZETOON_BASE_URL = "https://zebzetoon.vercel.app"
ZEBZETOON_CDN_BASE = "https://cdn.jsdelivr.net/gh/toonarc/kapaklar/"

# ───────────────────────────────────────────────
# ZebzeToon VERİ YAPISI
# ───────────────────────────────────────────────
# CSV cache (bellekte tutulacak)
series_cache = {}
cache_timestamp = None
CACHE_DURATION = 300  # 5 dakika

# Son bölüm takibi (otomatik duyuru için)
# {seri_adı: son_bölüm_numarası}
last_chapters = {}


# ───────────────────────────────────────────────
# ZebzeToon CSV OKUMA FONKSİYONU
# ───────────────────────────────────────────────
def fetch_zebzetoon_data():
    """
    ZebzeToon'dan liste.csv dosyasını çeker ve parse eder.
    CSV yapısı: İsim, Klasör, User, Repo, Aralık, Kapak, Banner, Tür, Durum, Yazar, Özet, Puan, Tarih, Kilitli, KilitliBolumSayisi
    """
    global series_cache, cache_timestamp
    
    # Cache kontrolü
    if cache_timestamp and (datetime.now().timestamp() - cache_timestamp < CACHE_DURATION):
        return series_cache
    
    try:
        response = requests.get(ZEBZETOON_CSV_URL, timeout=10)
        response.raise_for_status()
        
        # CSV parse et
        lines = response.text.strip().split('\n')
        if len(lines) < 2:
            print("[fetch_zebzetoon_data] CSV boş veya geçersiz")
            return {}
        
        # Header'ı atla
        data_lines = lines[1:]
        
        series_data = {}
        for line in data_lines:
            # Virgülle ayır ama özet içindeki virgülleri koru
            # Basit CSV parser - 15 alan bekliyoruz
            parts = line.split(',', 14)  # İlk 14 virgülde böl, kalanı son alana koy
            
            if len(parts) < 15:
                continue
            
            series_name = parts[0].strip()
            
            # Boş satırları atla
            if not series_name:
                continue
            
            series_data[series_name.lower()] = {
                'isim': parts[0].strip(),
                'klasor': parts[1].strip(),
                'user': parts[2].strip(),
                'repo': parts[3].strip(),
                'aralik': parts[4].strip(),
                'kapak': parts[5].strip(),
                'banner': parts[6].strip(),
                'tur': parts[7].strip(),
                'durum': parts[8].strip(),
                'yazar': parts[9].strip(),
                'ozet': parts[10].strip(),
                'puan': parts[11].strip(),
                'tarih': parts[12].strip(),
                'kilitli': parts[13].strip(),
                'kilitliBolumSayisi': parts[14].strip()
            }
        
        series_cache = series_data
        cache_timestamp = datetime.now().timestamp()
        print(f"[fetch_zebzetoon_data] {len(series_data)} seri yüklendi")
        return series_data
        
    except Exception as e:
        print(f"[fetch_zebzetoon_data] Hata: {e}")
        return series_cache  # Eski cache'i döndür


def get_cover_image_url(kapak_path):
    """
    Kapak resmi yolunu tam URL'ye çevirir.
    - Eğer http ile başlıyorsa olduğu gibi kullan
    - Değilse CDN base'i ekle ve 'kapaklar/' prefix'ini kaldır
    """
    if not kapak_path:
        return None
    
    if kapak_path.startswith('http'):
        return kapak_path
    
    # 'kapaklar/' prefix'ini kaldır
    clean_path = kapak_path.replace('kapaklar/', '')
    return f"{ZEBZETOON_CDN_BASE}{clean_path}"


def parse_chapter_range(aralik):
    """
    Bölüm aralığını parse eder ve son bölüm numarasını döndürür.
    Örnek: "1-8" -> 8, "1-40" -> 40
    """
    if not aralik or '-' not in aralik:
        return None
    
    try:
        parts = aralik.split('-')
        return int(parts[1])
    except (ValueError, IndexError):
        return None


# ───────────────────────────────────────────────
# SERİ THREAD'İ OLUŞTUR VEYA BUL
# ───────────────────────────────────────────────
async def get_or_create_series_thread(guild, series_name, cover_url=None, status=None, genres=None):
    """
    Text channel altında seri için thread bulur veya oluşturur.
    """
    if not series_name or not SERIES_THREAD_CHANNEL_ID:
        return None
    
    parent_channel = guild.get_channel(SERIES_THREAD_CHANNEL_ID)
    if not parent_channel:
        print(f"[get_or_create_series_thread] Kanal bulunamadı: {SERIES_THREAD_CHANNEL_ID}")
        return None
    
    series_lower = series_name.lower()
    
    # Mevcut thread'leri kontrol et
    if hasattr(parent_channel, 'threads'):
        for thread in parent_channel.threads:
            if thread.name.lower() == series_lower:
                return thread
    
    # Aktif thread'leri kontrol et
    try:
        active_threads = await guild.active_threads()
        for thread in active_threads:
            if thread.parent_id == SERIES_THREAD_CHANNEL_ID and thread.name.lower() == series_lower:
                return thread
    except Exception as e:
        print(f"[get_or_create_series_thread] Aktif thread hatası: {e}")
    
    # Arşivlenmiş thread'leri kontrol et
    try:
        if hasattr(parent_channel, 'archived_threads'):
            async for thread in parent_channel.archived_threads(limit=100):
                if thread.name.lower() == series_lower:
                    return thread
    except Exception as e:
        print(f"[get_or_create_series_thread] Arşiv thread hatası: {e}")
    
    # Yeni thread oluştur
    try:
        # Durum rengi
        embed_color = 0x00BFFF
        if status and "Devam" in status:
            embed_color = 0x00FF7F
        elif status and "Tamamlandı" in status:
            embed_color = 0xFFD700
        elif status and "Bırakıldı" in status:
            embed_color = 0xFF4500
        
        # İlk mesaj embed'i
        desc_parts = []
        if status:
            desc_parts.append(f"**{status}**")
        if genres:
            desc_parts.append(f"🏷️ {genres}")
        desc_parts.append("Yeni bölümler burada paylaşılacak!")
        
        embed = discord.Embed(
            title=f"📚 {series_name}",
            description="\n".join(desc_parts),
            color=embed_color,
        )
        if cover_url:
            embed.set_thumbnail(url=cover_url)
        
        # Thread oluştur
        if isinstance(parent_channel, discord.TextChannel):
            msg = await parent_channel.send(embed=embed)
            thread = await msg.create_thread(name=series_name)
            print(f"[get_or_create_series_thread] Yeni thread oluşturuldu: {series_name}")
            return thread
        
    except Exception as e:
        print(f"[get_or_create_series_thread] Thread oluşturma hatası: {e}")
    
    return None


# ───────────────────────────────────────────────
# BOT AÇILDIĞINDA
# ───────────────────────────────────────────────
@client.event
async def on_ready():
    print("ZebzeToon Discord Bot aktif!")
    # İlk veri yüklemesi
    fetch_zebzetoon_data()
    # Otomatik duyuru task'ını başlat
    check_new_chapters.start()
    await client.change_presence(activity=discord.Game(name="Manga Okuyor..."))


# ───────────────────────────────────────────────
# ZebzeToon LINK YAKALAMA
# ───────────────────────────────────────────────
@client.event
async def on_message(message):
    # Bot'un kendi mesajlarını ignore et
    if message.author.bot:
        return
    
    # ZebzeToon linki var mı kontrol et
    zebzetoon_pattern = r'https?://zebzetoon\.vercel\.app/\?seri=([^&\s]+)(?:&bolum=(\d+))?'
    matches = re.findall(zebzetoon_pattern, message.content)
    
    if matches:
        # Veriyi çek
        series_data = fetch_zebzetoon_data()
        
        for match in matches:
            series_name_encoded = match[0]
            chapter_num = match[1] if match[1] else None
            
            # URL decode
            series_name = unquote(series_name_encoded)
            
            # Seriyi bul
            series_info = series_data.get(series_name.lower())
            
            if not series_info:
                print(f"[on_message] Seri bulunamadı: {series_name}")
                continue
            
            # Kapak resmini al
            cover_url = get_cover_image_url(series_info['kapak'])
            
            # Embed oluştur
            embed = discord.Embed(
                title=f"📖 {series_info['isim']}",
                description=series_info['ozet'][:200] + "..." if len(series_info['ozet']) > 200 else series_info['ozet'],
                color=random.choice(EMBED_COLORS),
            )
            
            # Seri bilgileri
            embed.add_field(
                name="📚 Seri",
                value=f"`{series_info['isim']}`",
                inline=True,
            )
            
            if chapter_num:
                embed.add_field(
                    name="📄 Bölüm",
                    value=f"`{chapter_num}`",
                    inline=True,
                )
            
            # Durum
            embed.add_field(
                name="📊 Durum",
                value=series_info['durum'],
                inline=True,
            )
            
            # Tür
            if series_info['tur']:
                embed.add_field(
                    name="🏷️ Tür",
                    value=series_info['tur'],
                    inline=False,
                )
            
            # Kapak resmi
            if cover_url:
                embed.set_image(url=cover_url)
            
            embed.set_footer(
                text="Zebze Toon",
            )
            
            # Buton ekle
            view = discord.ui.View()
            link_url = f"{ZEBZETOON_BASE_URL}/?seri={series_name_encoded}"
            if chapter_num:
                link_url += f"&bolum={chapter_num}"
            
            view.add_item(discord.ui.Button(
                label="📖 Oku",
                style=discord.ButtonStyle.link,
                url=link_url
            ))
            
            await message.channel.send(embed=embed, view=view)
    
    # Komutları işle
    await client.process_commands(message)


# ───────────────────────────────────────────────
# ++seriler KOMUTU - Tüm serileri listele
# ───────────────────────────────────────────────
@client.command()
async def seriler(ctx):
    """ZebzeToon'daki tüm serileri listeler"""
    try:
        loading_msg = await ctx.send("📚 Seriler yükleniyor...")
        
        # Veriyi çek
        series_data = fetch_zebzetoon_data()
        
        if not series_data:
            await loading_msg.edit(content="❌ Hiç seri bulunamadı.")
            return
        
        await loading_msg.delete()
        
        # Her seri için embed gönder
        for series_key, series_info in series_data.items():
            # Kapak resmini al
            cover_url = get_cover_image_url(series_info['kapak'])
            
            # Durum rengi
            embed_color = 0x00BFFF  # Varsayılan mavi
            if "Devam" in series_info['durum']:
                embed_color = 0x00FF7F  # Yeşil
            elif "Tamamlandı" in series_info['durum']:
                embed_color = 0xFFD700  # Altın
            elif "Bırakıldı" in series_info['durum']:
                embed_color = 0xFF4500  # Kırmızı
            
            # Özet kısalt
            ozet = series_info['ozet'][:150]
            if len(series_info['ozet']) > 150:
                ozet += "..."
            
            embed = discord.Embed(
                title=series_info['isim'],
                description=f"**Özet:**\n{ozet}" if ozet else "",
                color=embed_color,
            )
            
            # Field'lar
            embed.add_field(name="Durum", value=series_info['durum'], inline=True)
            embed.add_field(name="Türler", value=series_info['tur'] or "—", inline=True)
            
            # Thumbnail
            if cover_url:
                embed.set_thumbnail(url=cover_url)
            
            # Buton
            view = discord.ui.View()
            from urllib.parse import quote
            series_url = f"{ZEBZETOON_BASE_URL}/?seri={quote(series_info['isim'])}"
            view.add_item(discord.ui.Button(
                label="📚 Seriye Git",
                style=discord.ButtonStyle.link,
                url=series_url
            ))
            
            await ctx.send(embed=embed, view=view)
        
        await ctx.send(f"📊 Toplam **{len(series_data)}** seri listelendi!")
        
    except Exception as e:
        print(f"[seriler] Hata: {e}")
        await ctx.send("❌ Seriler yüklenirken hata oluştu.")


# ───────────────────────────────────────────────
# ++seri KOMUTU - Tek seri göster
# ───────────────────────────────────────────────
@client.command()
async def seri(ctx, *, seri_adi: str = None):
    """Belirtilen seriyi gösterir. Kullanım: ++seri Ölüm Paktı"""
    if not seri_adi:
        await ctx.send("❌ Kullanım: `++seri <seri adı>`\nÖrnek: `++seri Ölüm Paktı`")
        return
    
    try:
        # Veriyi çek
        series_data = fetch_zebzetoon_data()
        
        # Seriyi bul
        series_info = series_data.get(seri_adi.lower())
        
        if not series_info:
            await ctx.send(f"❌ **{seri_adi}** adında seri bulunamadı.")
            return
        
        # Kapak resmini al
        cover_url = get_cover_image_url(series_info['kapak'])
        
        # Durum rengi
        embed_color = 0x00BFFF
        if "Devam" in series_info['durum']:
            embed_color = 0x00FF7F
        elif "Tamamlandı" in series_info['durum']:
            embed_color = 0xFFD700
        elif "Bırakıldı" in series_info['durum']:
            embed_color = 0xFF4500
        
        embed = discord.Embed(
            title=f"📚 {series_info['isim']}",
            description=f"**{series_info['durum']}**\n\n🏷️ {series_info['tur']}\n\n{series_info['ozet']}",
            color=embed_color,
        )
        
        # Büyük kapak resmi
        if cover_url:
            embed.set_image(url=cover_url)
        
        embed.set_footer(text="Zebze Toon • ++seri")
        
        # Buton
        view = discord.ui.View()
        from urllib.parse import quote
        series_url = f"{ZEBZETOON_BASE_URL}/?seri={quote(series_info['isim'])}"
        view.add_item(discord.ui.Button(
            label="📚 Seriye Git",
            style=discord.ButtonStyle.link,
            url=series_url
        ))
        
        await ctx.send(embed=embed, view=view)
        
    except Exception as e:
        print(f"[seri] Hata: {e}")
        await ctx.send("❌ Seri yüklenirken hata oluştu.")


# ───────────────────────────────────────────────
# OTOMATİK YENİ BÖLÜM KONTROLÜ
# ───────────────────────────────────────────────
@tasks.loop(minutes=10)
async def check_new_chapters():
    """
    Her 10 dakikada bir CSV'yi kontrol eder ve yeni bölüm varsa duyuru yapar
    """
    global last_chapters
    
    try:
        # Veriyi çek
        series_data = fetch_zebzetoon_data()
        
        if not series_data:
            return
        
        # Kanal kontrolü
        channel = client.get_channel(CHANNEL_ID)
        if not channel:
            print(f"[check_new_chapters] Kanal bulunamadı: {CHANNEL_ID}")
            return
        
        # Guild al
        guild = channel.guild
        
        # Her seri için kontrol
        for series_key, series_info in series_data.items():
            series_name = series_info['isim']
            current_chapter = parse_chapter_range(series_info['aralik'])
            
            if current_chapter is None:
                continue
            
            # İlk çalışma - sadece kaydet, duyuru yapma
            if series_name not in last_chapters:
                last_chapters[series_name] = current_chapter
                continue
            
            # Yeni bölüm kontrolü
            if current_chapter > last_chapters[series_name]:
                print(f"[check_new_chapters] Yeni bölüm bulundu: {series_name} - Bölüm {current_chapter}")
                
                # Kapak resmini al
                cover_url = get_cover_image_url(series_info['kapak'])
                
                # Seri thread'ini bul veya oluştur
                series_thread = await get_or_create_series_thread(
                    guild, 
                    series_name, 
                    cover_url, 
                    series_info['durum'], 
                    series_info['tur']
                )
                
                # Embed oluştur
                embed = discord.Embed(
                    title=f"� {series_name}",
                    description=f"**Bölüm {current_chapter}** yayınlandı!\n\n"
                               f"━━━━━━━━━━━━━━━━━━━━━━",
                    color=random.choice(EMBED_COLORS),
                )
                
                # Seri ve bölüm bilgisi
                embed.add_field(
                    name="📚 Seri",
                    value=f"`{series_name}`",
                    inline=True,
                )
                
                embed.add_field(
                    name="📄 Bölüm",
                    value=f"`{current_chapter}`",
                    inline=True,
                )
                
                # Boş alan
                embed.add_field(
                    name="\u200b",
                    value="\u200b",
                    inline=True,
                )
                
                # Kapak resmi
                if cover_url:
                    embed.set_image(url=cover_url)
                
                embed.set_footer(text="Zebze Toon")
                
                # Buton ekle
                view = discord.ui.View()
                from urllib.parse import quote
                chapter_url = f"{ZEBZETOON_BASE_URL}/?seri={quote(series_name)}&bolum={current_chapter}"
                view.add_item(discord.ui.Button(
                    label="📖 Oku",
                    style=discord.ButtonStyle.link,
                    url=chapter_url
                ))
                
                # Duyuru gönder - hem ana kanala hem thread'e
                await channel.send(embed=embed, view=view)
                
                if series_thread:
                    await series_thread.send(embed=embed, view=view)
                
                # Son bölümü güncelle
                last_chapters[series_name] = current_chapter
        
    except Exception as e:
        print(f"[check_new_chapters] Hata: {e}")


@check_new_chapters.before_loop
async def before_check_new_chapters():
    """Task başlamadan önce bot'un hazır olmasını bekle"""
    await client.wait_until_ready()
    # İlk çalışmada mevcut bölümleri kaydet
    series_data = fetch_zebzetoon_data()
    for series_key, series_info in series_data.items():
        series_name = series_info['isim']
        current_chapter = parse_chapter_range(series_info['aralik'])
        if current_chapter:
            last_chapters[series_name] = current_chapter
    print("[check_new_chapters] Otomatik bölüm kontrolü başlatıldı")


# ───────────────────────────────────────────────
# BOT BAŞLAT
# ───────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("HATA: DISCORD_TOKEN environment variable tanımlı değil!")
    else:
        client.run(DISCORD_TOKEN)
