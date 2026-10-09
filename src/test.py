import dbtx.docs.catalog as dbtx_catalog

catalog = dbtx_catalog.DocsCatalog()
catalog.parse_manifest(r"C:\Users\Martin\Documents\repos\dbt-add-in\examples\jaffle_shop\target\manifest.json")

print(catalog.metadata)
for index,dic in enumerate(catalog.nodes.items()):
    if index>=10:
        break
    print(index, dic[0], dic[1])
print('---')

catalog.parse_run(r"C:\Users\Martin\Documents\repos\dbt-add-in\examples\jaffle_shop\target\run_results.json")
for index,dic in enumerate(catalog.nodes.items()):
    if index >=10:
        break
    print(index, dic[0], (dic[1]).run_result) if dic[1].run_result else print(index, dic[0], 'No Run Result')
print('---')