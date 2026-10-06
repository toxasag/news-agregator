import asyncio
from collections import defaultdict
from app.database import AsyncSessionLocal
from app.services.wp_connection import load_wp_connection, _resolve_ssh_keys, _establish_ssh

SEO_PHP_SNIPPET = """
/**
 * SEO: enforce noindex, follow on paginated subpages (/page/2/, /page/3/, etc.)
 * Prevents search engines (Yandex, Google) from flagging duplicated titles & descriptions.
 */
add_filter( 'rank_math/frontend/robots', function( $robots ) {
    if ( is_paged() ) {
        $robots['index']  = 'noindex';
        $robots['follow'] = 'follow';
    }
    return $robots;
} );

add_filter( 'wp_robots', function( $robots ) {
    if ( is_paged() ) {
        $robots['noindex'] = true;
        $robots['follow']  = true;
    }
    return $robots;
} );
"""

async def run():
    async with AsyncSessionLocal() as session:
        cfg = await load_wp_connection(session)
    keys, tmp_p = _resolve_ssh_keys(cfg)
    try:
        conn, err = await _establish_ssh(cfg, keys)
        if not conn:
            print("SSH error:", err)
            return
        async with conn:
            wp_dir = cfg.wp_document_root
            theme_func = f"{wp_dir}/wp-content/themes/svodka-setka/functions.php"
            
            # 1. Check if SEO filter is present
            check_r = await conn.run(f"grep -q 'rank_math/frontend/robots' {theme_func} && echo 'EXISTS' || echo 'NOT_FOUND'")
            if "EXISTS" not in check_r.stdout:
                # Append snippet safely via sftp or EOF
                await conn.run(f"cat << 'EOF' >> {theme_func}\n{SEO_PHP_SNIPPET}\nEOF")
                print("Theme functions.php updated with noindex filter.")
            else:
                print("SEO filter already present in functions.php on WP server.")

            # 2. Query duplicate posts
            find_sql = """
                SELECT ID, post_title, post_date
                FROM wp_posts
                WHERE post_type='post' AND post_status='publish'
                AND post_title IN (
                    SELECT post_title 
                    FROM wp_posts 
                    WHERE post_type='post' AND post_status='publish' 
                    GROUP BY post_title 
                    HAVING count(*) > 1
                )
                ORDER BY post_title, post_date ASC, ID ASC;
            """
            r = await conn.run(f"cd {wp_dir} && wp db query \"{find_sql}\" --allow-root")
            lines = [l for l in r.stdout.strip().split("\n") if l.strip()]
            groups = defaultdict(list)
            for line in lines[1:]:
                parts = line.split("\t")
                if len(parts) >= 3:
                    groups[parts[1]].append(parts[0])

            trash_ids = []
            for title, ids in groups.items():
                trash_ids.extend(ids[1:])

            print(f"Found {len(groups)} duplicate groups, total {len(trash_ids)} duplicate posts to trash.")
            if trash_ids:
                batch_size = 100
                for i in range(0, len(trash_ids), batch_size):
                    batch = trash_ids[i:i+batch_size]
                    in_clause = ",".join(batch)
                    update_sql = f"UPDATE wp_posts SET post_status='trash' WHERE ID IN ({in_clause});"
                    await conn.run(f"cd {wp_dir} && wp db query \"{update_sql}\" --allow-root")
                print(f"Successfully moved {len(trash_ids)} duplicate posts to trash!")
                await conn.run(f"cd {wp_dir} && wp cache flush --allow-root")
                print("WP Object Cache flushed.")
            else:
                print("No duplicate posts to trash.")
    finally:
        if tmp_p and tmp_p.exists():
            tmp_p.unlink()

if __name__ == "__main__":
    asyncio.run(run())
