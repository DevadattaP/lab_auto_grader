#include<stdio.h>

int main()
{
  int m;
  scanf("%d",&m);
  if(m>=0 && m<40)
    printf("FAIL");
  else if(m>=40 && m<=100)
    printf("PASS");
  else
    printf("INVALID");
  return 0;
}
