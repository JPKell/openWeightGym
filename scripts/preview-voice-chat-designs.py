"""Render three standalone two-way browser Voice studies with prepared sample calls."""
import argparse
import re
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


def sample_data():
    """Illustrative configured capabilities; no discovery or inference is performed."""
    languages = dict(en='English', es='Spanish', fr='French', de='German', ja='Japanese')
    return dict(languages=languages,
        recognition=[dict(id='qwen-asr', name='Qwen3 ASR', languages=list(languages)),
                     dict(id='parakeet', name='Parakeet · English profile', languages=['en'])],
        conversation=[dict(id='qwen', name='Qwen3 · 8B'), dict(id='llama', name='Llama · 8B')],
        synthesis=[dict(id='qwen-tts', name='Qwen3 TTS', languages=list(languages), voices=[
            dict(id='ryan', name='Ryan', description='Clear · conversational'),
            dict(id='vivian', name='Vivian', description='Warm · expressive'),
            dict(id='serena', name='Serena', description='Soft · measured')]),
            dict(id='kokoro', name='Kokoro · English profile', languages=['en'], voices=[
                dict(id='af_heart', name='Heart', description='Warm · American English'),
                dict(id='am_adam', name='Adam', description='Grounded · American English')])],
        turns=[
            dict(user=dict(en="I'd like to practise ordering a coffee. Can you help?", es='Me gustaría practicar cómo pedir un café. ¿Puedes ayudarme?', fr='Je voudrais apprendre à commander un café. Tu peux m’aider ?', de='Ich möchte üben, einen Kaffee zu bestellen. Kannst du mir helfen?', ja='コーヒーを注文する練習をしたいです。手伝ってくれますか？'),
                 assistant=dict(en='Of course. Let’s start at a small café. What would you like to order?', es='Claro. Empecemos en una pequeña cafetería. ¿Qué te gustaría pedir?', fr='Bien sûr. Commençons dans un petit café. Que voudrais-tu commander ?', de='Natürlich. Beginnen wir in einem kleinen Café. Was möchtest du bestellen?', ja='もちろんです。小さなカフェから始めましょう。何を注文したいですか？')),
            dict(user=dict(en='A coffee, please. Could I have it with oat milk?', es='Un café, por favor. ¿Podría ser con leche de avena?', fr='Un café, s’il vous plaît. Avec du lait d’avoine, si possible ?', de='Einen Kaffee, bitte. Könnte ich ihn mit Hafermilch haben?', ja='コーヒーをお願いします。オーツミルクにできますか？'),
                 assistant=dict(en='Absolutely. Try: “A coffee with oat milk, please.” Would you like to order something to eat as well?', es='Por supuesto. Prueba con: «Un café con leche de avena, por favor». ¿Te gustaría pedir algo de comer también?', fr='Absolument. Essaie : « Un café au lait d’avoine, s’il vous plaît. » Veux-tu commander quelque chose à manger aussi ?', de='Sehr gern. Versuch es mit: „Einen Kaffee mit Hafermilch, bitte.“ Möchtest du auch etwas zu essen bestellen?', ja='はい。「オーツミルクのコーヒーをお願いします」と言えます。食べ物も注文してみますか？')),
            dict(user=dict(en='Yes, a croissant would be lovely. How do I ask for the bill?', es='Sí, me gustaría un cruasán. ¿Cómo pido la cuenta?', fr='Oui, je prendrais un croissant. Comment demander l’addition ?', de='Ja, ein Croissant wäre schön. Wie bitte ich um die Rechnung?', ja='はい、クロワッサンもお願いします。お会計はどう頼めばいいですか？'),
                 assistant=dict(en='Try: “Could I have the bill, please?” That’s a friendly, natural way to finish your order.', es='Prueba con: «¿Me trae la cuenta, por favor?». Es una forma amable y natural de terminar tu pedido.', fr='Essaie : « Je pourrais avoir l’addition, s’il vous plaît ? » C’est une façon aimable de terminer ta commande.', de='Versuch es mit: „Könnte ich bitte die Rechnung haben?“ So schließt du deine Bestellung freundlich ab.', ja='「お会計をお願いします」と言ってみましょう。丁寧で自然な頼み方です。'))])


def render():
    static = ROOT / 'weightroom/static'
    pages = Environment(loader=FileSystemLoader(ROOT / 'weightroom/pages'), autoescape=select_autoescape())
    return pages.get_template('voice_chat_designs.html').render(
        css='\n'.join((static / name).read_text() for name in ('tabler.min.css', 'style.css', 'themes.css', 'voice-chat.css')),
        theme_javascript=(static / 'themes.js').read_text(), javascript=(static / 'voice-chat-designs.js').read_text(), data=sample_data(),
        icons_svg=re.sub(r'\bid="([^"]+)"', r'id="vc-icon-\1"', (static / 'icons.svg').read_text() + (static / 'voice-chat-icons.svg').read_text()),
    ).replace('/static/icons.svg?v=3#', '#vc-icon-').replace('/static/voice-chat-icons.svg?v=1#', '#vc-icon-')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/voice-chat-designs/index.html')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render())
    print(args.output.resolve())
