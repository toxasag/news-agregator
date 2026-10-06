<?php
/**
 * Main Feed Template (Home, Category, Tag, Search, Archive)
 *
 * Renders a continuous single-column Setka feed where news cards flow one after
 * another with native Telegram subscription callouts every 4 items.
 */

get_header();
?>

<main id="main-content" class="setka-main" role="main">

	<?php if ( is_archive() || is_search() ) : ?>
		<header class="setka-archive-header">
			<?php if ( is_search() ) : ?>
				<h1 class="setka-archive-header__title">
					Результаты поиска: «<?php echo esc_html( get_search_query() ); ?>»
				</h1>
			<?php else : ?>
				<h1 class="setka-archive-header__title">
					<?php single_term_title(); ?>
				</h1>
				<?php
				$term_desc = get_the_archive_description();
				if ( $term_desc ) :
					?>
					<div class="setka-archive-header__desc"><?php echo wp_kses_post( $term_desc ); ?></div>
				<?php endif; ?>
			<?php endif; ?>
		</header>
	<?php endif; ?>

	<?php if ( have_posts() ) : ?>
		<div id="svodka-feed" class="setka-feed" data-stream-mode="archive">
			<?php
			$card_index = 0;
			while ( have_posts() ) :
				the_post();
				$card_index++;
				$categories   = get_the_category();
				$primary_cat  = ! empty( $categories ) ? $categories[0] : null;
				$permalink    = get_permalink();
				$has_thumb    = has_post_thumbnail();
				?>
				<article id="post-<?php the_ID(); ?>" <?php post_class( 'setka-card svodka-feed-item' ); ?>>
					<div class="setka-card__meta">
						<?php if ( $primary_cat ) : ?>
							<a class="setka-card__cat" href="<?php echo esc_url( get_category_link( $primary_cat->term_id ) ); ?>">
								<?php echo esc_html( $primary_cat->name ); ?>
							</a>
						<?php else : ?>
							<span class="setka-card__cat">Новости</span>
						<?php endif; ?>

						<time class="setka-card__time" datetime="<?php echo esc_attr( get_the_date( 'c' ) ); ?>">
							<?php echo esc_html( svodka_setka_time_ago( get_the_ID() ) ); ?>
						</time>
					</div>

					<h2 class="setka-card__title">
						<a href="<?php echo esc_url( $permalink ); ?>" rel="bookmark">
							<?php the_title(); ?>
						</a>
					</h2>

					<?php if ( $has_thumb ) : ?>
						<a class="setka-card__media" href="<?php echo esc_url( $permalink ); ?>" tabindex="-1" aria-hidden="true">
							<?php
							the_post_thumbnail(
								'medium_large',
								array(
									'loading'  => ( $card_index <= 2 ) ? 'eager' : 'lazy',
									'decoding' => 'async',
								)
							);
							?>
						</a>
					<?php endif; ?>

					<p class="setka-card__excerpt">
						<?php echo esc_html( wp_strip_all_tags( get_the_excerpt() ) ); ?>
					</p>

					<div class="setka-card__footer">
						<a class="setka-card__readmore" href="<?php echo esc_url( $permalink ); ?>">
							<span>Читать полностью</span>
							<span aria-hidden="true">→</span>
						</a>
					</div>
				</article>

				<?php
				// Inject rotating native advertiser card (Telegram -> FreeAPI AI -> OutVPN) after each card
				$paged          = max( 1, intval( get_query_var( 'paged' ) ) );
				$posts_per_page = intval( get_query_var( 'posts_per_page' ) );
				if ( $posts_per_page <= 0 ) {
					$posts_per_page = intval( get_option( 'posts_per_page', 10 ) );
				}
				$paged_offset = ( $paged - 1 ) * $posts_per_page;
				$slot_index   = ( $card_index - 1 ) + $paged_offset;
				svodka_setka_render_rotating_promo( $slot_index );
			endwhile;
			?>
		</div>

		<?php
		$next_page_url = get_next_posts_page_link();
		if ( $next_page_url && $GLOBALS['wp_query']->max_num_pages > max( 1, get_query_var( 'paged' ) ) ) :
			?>
			<div id="svodka-sentinel" data-next-url="<?php echo esc_url( $next_page_url ); ?>">
				Загружаем следующие новости…
			</div>
		<?php endif; ?>

		<nav class="setka-pagination" aria-label="Страницы ленты">
			<?php
			echo paginate_links(
				array(
					'prev_text' => '← Назад',
					'next_text' => 'Дальше →',
				)
			);
			?>
		</nav>

	<?php else : ?>
		<div class="setka-card">
			<h2 class="setka-card__title">Материалы не найдены</h2>
			<p class="setka-card__excerpt">Попробуйте выбрать другую рубрику в верхней панели или изменить поисковый запрос.</p>
		</div>
	<?php endif; ?>

</main>

<?php
get_footer();
