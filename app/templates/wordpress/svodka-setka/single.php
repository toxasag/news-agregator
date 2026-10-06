<?php
/**
 * Single Post Template — Infinite Scroll News Stream
 *
 * Renders the current news article wrapped in `#svodka-feed` and `.svodka-post`,
 * followed by `#svodka-sentinel` pointing to the chronologically previous (newer/older)
 * post so readers scroll from one story directly into the next without page reloads.
 */

get_header();
?>

<main id="main-content" class="setka-main" role="main">
	<?php
	while ( have_posts() ) :
		the_post();
		$post_id      = get_the_ID();
		$permalink    = get_permalink( $post_id );
		$relative_url = wp_make_link_relative( $permalink );
		$categories   = get_the_category( $post_id );
		$primary_cat  = ! empty( $categories ) ? $categories[0] : null;

		// Next story in the feed (the adjacent older post, or newer if none)
		$next_story = get_previous_post();
		if ( ! $next_story ) {
			$next_story = get_next_post();
		}
		$next_story_url = $next_story ? get_permalink( $next_story->ID ) : '';
		?>
		<div id="svodka-feed" data-stream-mode="single">
			<article
				id="post-<?php echo esc_attr( $post_id ); ?>"
				<?php post_class( 'svodka-post' ); ?>
				data-post-url="<?php echo esc_attr( $relative_url ); ?>"
				data-post-title="<?php echo esc_attr( wp_strip_all_tags( get_the_title() ) ); ?>"
			>
				<header class="svodka-post__header">
					<div class="svodka-post__meta">
						<?php if ( $primary_cat ) : ?>
							<a class="setka-card__cat" href="<?php echo esc_url( get_category_link( $primary_cat->term_id ) ); ?>">
								<?php echo esc_html( $primary_cat->name ); ?>
							</a>
						<?php endif; ?>

						<time class="setka-card__time" datetime="<?php echo esc_attr( get_the_date( 'c' ) ); ?>">
							<?php echo esc_html( svodka_setka_time_ago( $post_id ) ); ?>
						</time>
					</div>

					<h1 class="svodka-post__title"><?php the_title(); ?></h1>

					<?php if ( has_post_thumbnail( $post_id ) ) : ?>
						<figure class="svodka-post__hero">
							<?php
							the_post_thumbnail(
								'large',
								array(
									'loading'  => 'eager',
									'decoding' => 'async',
								)
							);
							if ( function_exists( 'svodka_setka_render_hero_caption' ) ) {
								svodka_setka_render_hero_caption( $post_id );
							}
							?>
						</figure>
					<?php endif; ?>
				</header>

				<div class="svodka-post__content entry-content">
					<?php the_content(); ?>
				</div>

				<?php
				// Strictly 1 rotating promo card per post (Telegram -> FreeAPI AI -> OutVPN), matching the home feed
				svodka_setka_render_rotating_promo( $post_id );

				$tags = get_the_tags( $post_id );
				if ( $tags && ! is_wp_error( $tags ) ) :
					?>
					<div class="svodka-post__tags" aria-label="Теги материала">
						<?php foreach ( array_slice( $tags, 0, 8 ) as $tag ) : ?>
							<a class="svodka-post__tag" href="<?php echo esc_url( get_tag_link( $tag->term_id ) ); ?>">
								#<?php echo esc_html( $tag->name ); ?>
							</a>
						<?php endforeach; ?>
					</div>
				<?php endif; ?>

				<?php if ( $next_story ) : ?>
					<div class="setka-stream-divider" aria-hidden="true">
						<span>Следующая новость</span>
					</div>
				<?php endif; ?>
			</article>
		</div>

		<?php if ( $next_story_url ) : ?>
			<div id="svodka-sentinel" data-next-url="<?php echo esc_url( $next_story_url ); ?>">
				Загружаем следующую новость: «<?php echo esc_html( wp_strip_all_tags( get_the_title( $next_story->ID ) ) ); ?>»…
			</div>
		<?php endif; ?>

	<?php endwhile; ?>
</main>

<?php
get_footer();
