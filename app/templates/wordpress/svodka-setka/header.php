<!doctype html>
<html <?php language_attributes(); ?>>
<head>
	<meta charset="<?php bloginfo( 'charset' ); ?>">
	<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
	<?php
	$icons_uri = get_template_directory_uri() . '/assets/icons';
	?>
	<link rel="icon" href="<?php echo esc_url( $icons_uri . '/favicon.ico' ); ?>" sizes="32x32">
	<link rel="icon" href="<?php echo esc_url( $icons_uri . '/svodka-favicon.svg' ); ?>" type="image/svg+xml">
	<link rel="apple-touch-icon" href="<?php echo esc_url( $icons_uri . '/apple-touch-icon.png' ); ?>">
	<script>
	(function() {
		try {
			var saved = localStorage.getItem('svodka_theme');
			if (saved === 'dark' || saved === 'light') {
				document.documentElement.setAttribute('data-theme', saved);
			}
		} catch (e) {}
	})();
	</script>
	<?php wp_head(); ?>
</head>
<body <?php body_class(); ?>>
<?php wp_body_open(); ?>

<a class="skip-link" href="#main-content">Перейти к ленте новостей</a>

<header class="setka-header" role="banner">
	<div class="setka-header__inner">
		<a class="setka-brand" href="<?php echo esc_url( home_url( '/' ) ); ?>" rel="home">
			<span class="setka-brand__logo" aria-hidden="true">
				<svg viewBox="0 0 256 256" width="32" height="32" role="img" focusable="false">
					<path fill="#0070FF" fill-rule="evenodd" d="M 72 16 H 184 C 214.9 16 240 41.1 240 72 V 184 C 240 214.9 214.9 240 184 240 H 72 C 41.1 240 16 214.9 16 184 V 72 C 16 41.1 41.1 16 72 16 Z M 96 56 H 152 L 136 92 H 96 C 87.2 92 80 99.2 80 108 C 80 116.8 87.2 124 96 124 H 160 C 186.5 124 208 145.5 208 172 C 208 189.7 193.7 204 176 204 H 72 L 88 168 H 160 C 168.8 168 176 160.8 176 152 C 176 143.2 168.8 136 160 136 H 96 C 69.5 136 48 114.5 48 88 C 48 70.3 62.3 56 80 56 Z"/>
					<circle cx="192" cy="72" r="18" fill="#FF3B30"/>
				</svg>
			</span>
			<span class="setka-brand__wordmark">СВОДКА<span class="setka-brand__wordmark-accent">ПРЕСС</span></span>
		</a>

		<div class="setka-header__actions">
			<a class="setka-tg-pill" href="<?php echo esc_url( SVODKA_TG_URL ); ?>" target="_blank" rel="noopener noreferrer" aria-label="Подписаться на Telegram-канал Сводка Пресс">
				<svg viewBox="0 0 24 24" aria-hidden="true">
					<path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/>
				</svg>
				<span>Telegram</span>
			</a>

			<button type="button" class="setka-icon-btn" id="setka-search-toggle" aria-expanded="false" aria-controls="setka-search-drawer" aria-label="Поиск по новостям" title="Поиск">
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<circle cx="11" cy="11" r="8"></circle>
					<line x1="21" y1="21" x2="16.65" y2="16.65"></line>
				</svg>
			</button>

			<button type="button" class="setka-icon-btn" id="setka-theme-toggle" aria-label="Переключить цветовую тему (Светлая / Темная)" title="Тема оформления">
				<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
					<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
				</svg>
			</button>
		</div>
	</div>

	<div class="setka-search-bar" id="setka-search-drawer">
		<form role="search" method="get" class="setka-search-form" action="<?php echo esc_url( home_url( '/' ) ); ?>">
			<input type="search" class="setka-search-input" placeholder="Найти новость или тему…" value="<?php echo get_search_query(); ?>" name="s" aria-label="Поисковый запрос" />
			<button type="submit" class="setka-search-submit">Найти</button>
		</form>
	</div>

	<nav class="setka-cat-rail" aria-label="Рубрики новостей">
		<?php
		$current_cat_id = is_category() ? get_queried_object_id() : 0;
		$is_home_active = is_front_page() || is_home();
		?>
		<a class="setka-chip <?php echo $is_home_active ? 'is-active' : ''; ?>" href="<?php echo esc_url( home_url( '/' ) ); ?>">
			🔥 Все новости
		</a>
		<?php
		$top_categories = get_categories(
			array(
				'orderby'    => 'count',
				'order'      => 'DESC',
				'number'     => 14,
				'hide_empty' => true,
				'exclude'    => array( 1 ), // Exclude "Без рубрики"
			)
		);
		foreach ( $top_categories as $cat ) :
			$is_active = ( intval( $current_cat_id ) === intval( $cat->term_id ) );
			?>
			<a class="setka-chip <?php echo $is_active ? 'is-active' : ''; ?>" href="<?php echo esc_url( get_category_link( $cat->term_id ) ); ?>">
				<?php echo esc_html( $cat->name ); ?>
			</a>
		<?php endforeach; ?>
	</nav>
</header>

<div class="setka-layout-wrapper">
	<aside class="setka-sidebar-left" aria-label="Рубрики издания">
		<div class="setka-sidebar-left__inner">
			<div class="setka-sidebar-left__header">
				<span class="setka-sidebar-left__title">Рубрики</span>
				<span class="setka-sidebar-left__badge">24/7</span>
			</div>
			<nav class="setka-sidebar-nav" aria-label="Навигация по разделам">
				<a class="setka-sidebar-nav__item <?php echo ( is_front_page() || is_home() ) ? 'is-active' : ''; ?>" href="<?php echo esc_url( home_url( '/' ) ); ?>">
					<span class="setka-sidebar-nav__emoji" aria-hidden="true">🔥</span>
					<span class="setka-sidebar-nav__label">Все новости</span>
				</a>
				<?php
				$all_rubrics = svodka_setka_get_rubrics_catalog();
				$active_term_id = is_category() ? get_queried_object_id() : 0;
				foreach ( $all_rubrics as $rubric ) :
					$item_active = ( intval( $active_term_id ) === intval( $rubric['term_id'] ) );
					?>
					<a class="setka-sidebar-nav__item <?php echo $item_active ? 'is-active' : ''; ?>" href="<?php echo esc_url( $rubric['url'] ); ?>" title="<?php echo esc_attr( $rubric['full_name'] ); ?>">
						<span class="setka-sidebar-nav__emoji" aria-hidden="true"><?php echo esc_html( $rubric['emoji'] ); ?></span>
						<span class="setka-sidebar-nav__label"><?php echo esc_html( $rubric['name'] ); ?></span>
						<?php if ( ! empty( $rubric['count'] ) ) : ?>
							<span class="setka-sidebar-nav__count"><?php echo esc_html( $rubric['count'] ); ?></span>
						<?php endif; ?>
					</a>
				<?php endforeach; ?>
			</nav>
		</div>
	</aside>

