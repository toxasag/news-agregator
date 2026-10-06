<?php
/**
 * Static Page Template (О проекте, Контакты, Политика конфиденциальности)
 */

get_header();
?>

<main id="main-content" class="setka-main" role="main">
	<?php
	while ( have_posts() ) :
		the_post();
		?>
		<article id="page-<?php the_ID(); ?>" <?php post_class( 'svodka-post' ); ?>>
			<header class="svodka-post__header">
				<h1 class="svodka-post__title"><?php the_title(); ?></h1>
			</header>
			<div class="svodka-post__content entry-content">
				<?php the_content(); ?>
			</div>
			<?php svodka_setka_render_tg_card(); ?>
		</article>
	<?php endwhile; ?>
</main>

<?php
get_footer();
