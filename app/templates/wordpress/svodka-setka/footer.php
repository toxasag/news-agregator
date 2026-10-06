<?php
$contacts    = svodka_setka_get_contacts();
$contact_tg  = $contacts['contact_tg_url'];
$contact_tag = $contacts['contact_tg_handle'];
$contact_em  = $contacts['contact_email'];
$channel_tg  = $contacts['channel_tg_url'];
$channel_tag = $contacts['channel_tg_handle'];
?>
</div><!-- /.setka-layout-wrapper -->

<footer class="setka-footer" role="contentinfo">
	<div class="setka-footer__inner">
		<div class="setka-footer__row">
			<div>
				<strong>СВОДКА ПРЕСС</strong> — главные новости России и мира без лишней воды.
			</div>
			<a class="setka-tg-pill" href="<?php echo esc_url( $channel_tg ); ?>" target="_blank" rel="noopener noreferrer">
				<svg viewBox="0 0 24 24" aria-hidden="true">
					<path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/>
				</svg>
				<span>Наш Telegram <?php echo esc_html( $channel_tag ); ?></span>
			</a>
		</div>
		<div class="setka-footer__row">
			<span>&copy; <?php echo esc_html( gmdate( 'Y' ) ); ?> Сводка Пресс (svodka.press). Все права защищены.</span>
			<span>Связь и реклама: <a href="<?php echo esc_url( $contact_tg ); ?>" target="_blank" rel="noopener noreferrer"><?php echo esc_html( $contact_tag ); ?></a> · <a href="mailto:<?php echo esc_attr( $contact_em ); ?>"><?php echo esc_html( $contact_em ); ?></a></span>
		</div>
	</div>
</footer>

<!-- Sticky Bottom Action Bar: Предложить новость · Заказать рекламу · Контакты -->
<nav class="setka-bottom-bar" aria-label="Быстрые действия редакции">
	<div class="setka-bottom-bar__inner">
		<button type="button" class="setka-bottom-bar__btn" data-setka-sheet="suggest">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<path d="M12 20h9"></path>
				<path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path>
			</svg>
			<span>Предложить новость</span>
		</button>

		<button type="button" class="setka-bottom-bar__btn setka-bottom-bar__btn--accent" data-setka-sheet="ads">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>
			</svg>
			<span>Заказать рекламу</span>
		</button>

		<button type="button" class="setka-bottom-bar__btn" data-setka-sheet="contacts">
			<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
				<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
			</svg>
			<span>Контакты</span>
		</button>
	</div>
</nav>

<!-- Interactive Slide-Up Action Sheet -->
<div class="setka-sheet-backdrop" id="setka-sheet-backdrop" hidden>
	<div class="setka-sheet" role="dialog" aria-modal="true" aria-labelledby="setka-sheet-title">
		<div class="setka-sheet__header">
			<h3 class="setka-sheet__title" id="setka-sheet-title">Связь с редакцией</h3>
			<button type="button" class="setka-sheet__close" id="setka-sheet-close" aria-label="Закрыть окно">✕</button>
		</div>

		<div class="setka-sheet__panel" data-sheet-panel="suggest">
			<p class="setka-sheet__lead">
				Стали очевидцем важного события, располагаете эксклюзивными фото, видео или документами? Пришлите материал напрямую в редакцию «Сводка Пресс».
			</p>
			<ul class="setka-sheet__list">
				<li>Гарантируем полную анонимность источника по вашему запросу.</li>
				<li>Проверяем фактуру и публикуем срочные новости в течение 15 минут.</li>
			</ul>
			<div class="setka-sheet__actions">
				<a class="setka-tg-btn" href="<?php echo esc_url( $contact_tg ); ?>" target="_blank" rel="noopener noreferrer">
					<span>Отправить новость в Telegram (<?php echo esc_html( $contact_tag ); ?>)</span>
					<span aria-hidden="true">→</span>
				</a>
				<a class="setka-sheet__secondary-btn" href="mailto:<?php echo esc_attr( $contact_em ); ?>?subject=%D0%9F%D1%80%D0%B5%D0%B4%D0%BB%D0%BE%D0%B6%D0%B8%D1%82%D1%8C%20%D0%BD%D0%BE%D0%B2%D0%BE%D1%81%D1%82%D1%8C%20%D0%B2%20%D0%A1%D0%B2%D0%BE%D0%B4%D0%BA%D0%B0%20%D0%9F%D1%80%D0%B5%D1%81%D1%81">
					Написать на <?php echo esc_html( $contact_em ); ?>
				</a>
			</div>
		</div>

		<div class="setka-sheet__panel" data-sheet-panel="ads" hidden>
			<p class="setka-sheet__lead">
				Размещаем нативные интеграции, партнерские карточки в бесконечной ленте и спецпроекты на сайте <strong>svodka.press</strong>, в Telegram и Яндекс Дзен.
			</p>
			<ul class="setka-sheet__list">
				<li>Высокий CTR за счет нативной подачи в потоке чтения (формат Сетка UI).</li>
				<li>Сквозная UTM-разметка и прозрачный учет конверсий по целям Яндекс Метрики.</li>
			</ul>
			<div class="setka-sheet__actions">
				<a class="setka-tg-btn" href="<?php echo esc_url( $contact_tg ); ?>" target="_blank" rel="noopener noreferrer">
					<span>Обсудить размещение в Telegram (<?php echo esc_html( $contact_tag ); ?>)</span>
					<span aria-hidden="true">→</span>
				</a>
				<a class="setka-sheet__secondary-btn" href="mailto:<?php echo esc_attr( $contact_em ); ?>?subject=%D0%97%D0%B0%D0%BA%D0%B0%D0%B7%20%D1%80%D0%B5%D0%BA%D0%BB%D0%B0%D0%BC%D1%8B%20%D0%BD%D0%B0%20svodka.press">
					Запросить медиакит: <?php echo esc_html( $contact_em ); ?>
				</a>
			</div>
		</div>

		<div class="setka-sheet__panel" data-sheet-panel="contacts" hidden>
			<p class="setka-sheet__lead">
				Ежедневное информационно-аналитическое издание <strong>«Сводка Пресс»</strong> (svodka.press). Оперативные сводки, обзоры прессы и аналитика 24/7.
			</p>
			<ul class="setka-sheet__list">
				<li><strong>Официальный Telegram-канал:</strong> <a href="<?php echo esc_url( $channel_tg ); ?>" target="_blank" rel="noopener noreferrer"><?php echo esc_html( $channel_tag ); ?></a></li>
				<li><strong>Связь в Telegram (редакция и реклама):</strong> <a href="<?php echo esc_url( $contact_tg ); ?>" target="_blank" rel="noopener noreferrer"><?php echo esc_html( $contact_tag ); ?></a></li>
				<li><strong>Редакция и пресс-релизы:</strong> <a href="mailto:<?php echo esc_attr( $contact_em ); ?>"><?php echo esc_html( $contact_em ); ?></a></li>
				<li><strong>Рекламный отдел:</strong> <a href="mailto:<?php echo esc_attr( $contact_em ); ?>"><?php echo esc_html( $contact_em ); ?></a></li>
			</ul>
			<div class="setka-sheet__actions">
				<a class="setka-tg-btn" href="<?php echo esc_url( $contact_tg ); ?>" target="_blank" rel="noopener noreferrer">
					<span>Написать в Telegram <?php echo esc_html( $contact_tag ); ?></span>
					<span aria-hidden="true">→</span>
				</a>
				<a class="setka-sheet__secondary-btn" href="mailto:<?php echo esc_attr( $contact_em ); ?>">
					Написать на <?php echo esc_html( $contact_em ); ?>
				</a>
			</div>
		</div>
	</div>
</div>

<script>
(function() {
	var themeBtn = document.getElementById('setka-theme-toggle');
	if (themeBtn) {
		themeBtn.addEventListener('click', function() {
			var root = document.documentElement;
			var current = root.getAttribute('data-theme');
			var isDarkOS = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
			var activeDark = current === 'dark' || (!current && isDarkOS);
			var next = activeDark ? 'light' : 'dark';
			root.setAttribute('data-theme', next);
			try { localStorage.setItem('svodka_theme', next); } catch (e) {}
		});
	}

	var searchBtn = document.getElementById('setka-search-toggle');
	var searchDrawer = document.getElementById('setka-search-drawer');
	if (searchBtn && searchDrawer) {
		searchBtn.addEventListener('click', function() {
			var open = searchDrawer.classList.toggle('is-open');
			searchBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
			if (open) {
				var input = searchDrawer.querySelector('input');
				if (input) input.focus();
			}
		});
	}

	var backdrop = document.getElementById('setka-sheet-backdrop');
	var closeBtn = document.getElementById('setka-sheet-close');
	var titleEl = document.getElementById('setka-sheet-title');
	var triggers = document.querySelectorAll('[data-setka-sheet]');
	var panels = document.querySelectorAll('[data-sheet-panel]');
	var titles = {
		suggest: 'Предложить новость',
		ads: 'Заказать рекламу',
		contacts: 'Контакты редакции'
	};

	function openSheet(mode) {
		if (!backdrop) return;
		if (titleEl && titles[mode]) {
			titleEl.textContent = titles[mode];
		}
		for (var i = 0; i < panels.length; i++) {
			var p = panels[i];
			if (p.getAttribute('data-sheet-panel') === mode) {
				p.removeAttribute('hidden');
			} else {
				p.setAttribute('hidden', 'hidden');
			}
		}
		backdrop.removeAttribute('hidden');
		document.body.style.overflow = 'hidden';
	}

	function closeSheet() {
		if (!backdrop) return;
		backdrop.setAttribute('hidden', 'hidden');
		document.body.style.overflow = '';
	}

	for (var i = 0; i < triggers.length; i++) {
		triggers[i].addEventListener('click', function() {
			openSheet(this.getAttribute('data-setka-sheet'));
		});
	}

	if (closeBtn) {
		closeBtn.addEventListener('click', closeSheet);
	}
	if (backdrop) {
		backdrop.addEventListener('click', function(e) {
			if (e.target === backdrop) closeSheet();
		});
	}
	document.addEventListener('keydown', function(e) {
		if (e.key === 'Escape' && backdrop && !backdrop.hasAttribute('hidden')) {
			closeSheet();
		}
	});
})();
</script>

<?php wp_footer(); ?>
</body>
</html>
